import { useState, useEffect } from "react";
import {
  Card,
  CardContent,
  CardActions,
  Typography,
  Button,
  Box,
  Alert,
  CircularProgress,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogContentText,
  DialogActions,
  Chip,
} from "@mui/material";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { request, ApiError } from "../../api/client";
import { formatInNewYork } from "../resources/timeUtils";
import type { components } from "../../api/schema";

export type WaitEntry = components["schemas"]["WaitEntry"];
export type Booking = components["schemas"]["Booking"];

export interface OfferCardProps {
  entry: WaitEntry;
  resourceName?: string;
  onActionSuccess?: () => void;
}

export function OfferCard({ entry, resourceName, onActionSuccess }: OfferCardProps) {
  const queryClient = useQueryClient();
  const [secondsRemaining, setSecondsRemaining] = useState<number | null>(null);
  const [isAccepting, setIsAccepting] = useState(false);
  const [isDeclining, setIsDeclining] = useState(false);
  const [showDeclineConfirm, setShowDeclineConfirm] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [actionAnnouncement, setActionAnnouncement] = useState<string | null>(null);

  // Fetch linked booking via E11 to obtain server expires_at deadline (Spec 7.3, Revision 1.1)
  const {
    data: booking,
    isLoading: isBookingLoading,
    refetch: refetchBooking,
  } = useQuery<Booking>({
    queryKey: ["bookings", entry.offered_booking_id],
    queryFn: async () => {
      if (!entry.offered_booking_id) {
        throw new Error("No offered booking ID on waitlist entry");
      }
      const res = await request<Booking>(`/api/v1/bookings/${entry.offered_booking_id}`);
      return res.data;
    },
    enabled: Boolean(entry.offered_booking_id),
    refetchInterval: 15000,
  });

  // Countdown timer driven by server deadline (Spec 7.2, 7.3)
  useEffect(() => {
    if (!booking?.expires_at) {
      setSecondsRemaining(null);
      return;
    }

    const computeRemaining = () => {
      const deadline = new Date(booking.expires_at!).getTime();
      const diffSec = Math.max(0, Math.floor((deadline - Date.now()) / 1000));
      return diffSec;
    };

    const initialRemaining = computeRemaining();
    setSecondsRemaining(initialRemaining);

    if (initialRemaining <= 0) {
      // Bounded single refetch at initial deadline reached (R3)
      setActionAnnouncement("Offer deadline reached. Checking current status with server...");
      queryClient.invalidateQueries({ queryKey: ["waitlist", "mine"] });
      queryClient.invalidateQueries({ queryKey: ["bookings", entry.offered_booking_id] });
      queryClient.invalidateQueries({ queryKey: ["resources"] });
      refetchBooking();
      return;
    }

    let deadlineRefetched = false;
    const timer = setInterval(() => {
      const remaining = computeRemaining();
      setSecondsRemaining(remaining);

      if (remaining <= 0 && !deadlineRefetched) {
        deadlineRefetched = true;
        clearInterval(timer);
        // Bounded single refetch on timer expiration (Spec 7.2, 7.3, R3)
        setActionAnnouncement("Offer deadline reached. Checking current status with server...");
        queryClient.invalidateQueries({ queryKey: ["waitlist", "mine"] });
        queryClient.invalidateQueries({ queryKey: ["bookings", entry.offered_booking_id] });
        queryClient.invalidateQueries({ queryKey: ["resources"] });
        refetchBooking();
      }
    }, 1000);

    return () => clearInterval(timer);
  }, [booking?.expires_at, entry.offered_booking_id, queryClient, refetchBooking]);

  const handleAccept = async () => {
    setIsAccepting(true);
    setErrorMessage(null);

    try {
      // Accept via POST /api/v1/waitlist/{id}/accept with If-Match from entry version (Spec 4.2 E16, 7.3)
      await request<Booking>(`/api/v1/waitlist/${entry.id}/accept`, {
        method: "POST",
        headers: {
          "If-Match": `"${entry.version}"`,
          "Content-Type": "application/json",
        },
        body: {},
      });

      // Invalidate all affected queries
      queryClient.invalidateQueries({ queryKey: ["waitlist", "mine"] });
      queryClient.invalidateQueries({ queryKey: ["bookings", "mine"] });
      queryClient.invalidateQueries({ queryKey: ["resources"] });

      setActionAnnouncement("Offer accepted! Your reservation is now confirmed.");
      const stableTarget =
        document.getElementById("my-waitlist-heading") ||
        document.querySelector<HTMLElement>("[role='status']") ||
        document.querySelector<HTMLElement>("h1");
      if (stableTarget) {
        stableTarget.focus();
      }
      if (onActionSuccess) {
        onActionSuccess();
      }
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 412) {
          // Version mismatch: refetch entry and booking
          setErrorMessage(
            "This offer was updated or expired. Refreshing latest status..."
          );
          queryClient.invalidateQueries({ queryKey: ["waitlist", "mine"] });
          queryClient.invalidateQueries({ queryKey: ["bookings", entry.offered_booking_id] });
        } else if (err.status === 409) {
          if (err.code === "HOLD_EXPIRED") {
            setErrorMessage("This offer has expired. The slot has been released.");
          } else {
            setErrorMessage(err.message || "Unable to accept this offer.");
          }
          queryClient.invalidateQueries({ queryKey: ["waitlist", "mine"] });
          queryClient.invalidateQueries({ queryKey: ["resources"] });
        } else {
          setErrorMessage(err.message || "Failed to accept offer. Please try again.");
        }
      } else {
        setErrorMessage("Network error while accepting offer.");
      }
    } finally {
      setIsAccepting(false);
    }
  };

  const handleDecline = async () => {
    setIsDeclining(true);
    setErrorMessage(null);

    try {
      // Decline via DELETE /api/v1/waitlist/{id} with If-Match from entry version (Spec 4.2 E15, 7.3)
      await request<WaitEntry>(`/api/v1/waitlist/${entry.id}`, {
        method: "DELETE",
        headers: {
          "If-Match": `"${entry.version}"`,
        },
      });

      queryClient.invalidateQueries({ queryKey: ["waitlist", "mine"] });
      queryClient.invalidateQueries({ queryKey: ["resources"] });

      setActionAnnouncement("Offer declined.");
      const stableTarget =
        document.getElementById("my-waitlist-heading") ||
        document.querySelector<HTMLElement>("[role='status']") ||
        document.querySelector<HTMLElement>("h1");
      if (stableTarget) {
        stableTarget.focus();
      }
      setShowDeclineConfirm(false);
      if (onActionSuccess) {
        onActionSuccess();
      }
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 412) {
          setErrorMessage("This entry was already updated. Refreshing...");
          queryClient.invalidateQueries({ queryKey: ["waitlist", "mine"] });
        } else {
          setErrorMessage(err.message || "Failed to decline offer.");
        }
      } else {
        setErrorMessage("Network error while declining offer.");
      }
    } finally {
      setIsDeclining(false);
    }
  };

  const formatCountdown = (totalSec: number | null): string => {
    if (totalSec === null) return "--:--";
    const minutes = Math.floor(totalSec / 60);
    const seconds = totalSec % 60;
    return `${minutes.toString().padStart(2, "0")}:${seconds.toString().padStart(2, "0")}`;
  };

  const isExpiredCountdown = secondsRemaining !== null && secondsRemaining <= 0;

  return (
    <Card
      variant="outlined"
      sx={{
        mb: 2,
        borderColor: isExpiredCountdown ? "grey.400" : "warning.main",
        borderWidth: 2,
        backgroundColor: "#fffde7",
      }}
    >
      <CardContent>
        {actionAnnouncement && (
          <Alert severity="info" role="status" sx={{ mb: 2 }}>
            {actionAnnouncement}
          </Alert>
        )}

        {errorMessage && (
          <Alert severity="error" role="alert" sx={{ mb: 2 }}>
            {errorMessage}
          </Alert>
        )}

        <Box
          sx={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-start",
            flexWrap: "wrap",
            gap: 1,
            mb: 1,
          }}
        >
          <Box>
            <Chip label="Offer Available" color="warning" size="small" sx={{ mb: 1 }} />
            <Typography variant="h6" fontWeight="bold">
              {resourceName || "Reserved Resource"}
            </Typography>
          </Box>

          <Box sx={{ textAlign: "right" }}>
            <Typography variant="caption" color="text.secondary" display="block">
              Time Remaining to Claim:
            </Typography>
            <Typography
              variant="h5"
              fontWeight="bold"
              color={isExpiredCountdown ? "error.main" : "warning.dark"}
              aria-live="polite"
            >
              {isBookingLoading ? "Loading..." : formatCountdown(secondsRemaining)}
            </Typography>
          </Box>
        </Box>

        <Box sx={{ my: 1.5 }}>
          <Typography variant="body2" color="text.secondary">
            Offered Slot (America/New_York):
          </Typography>
          <Typography variant="body1" fontWeight="medium">
            {formatInNewYork(entry.starts_at, {
              weekday: "short",
              month: "short",
              day: "numeric",
              year: "numeric",
            })}
          </Typography>
          <Typography variant="body2" fontWeight="bold" color="primary.main">
            {formatInNewYork(entry.starts_at, {
              hour: "2-digit",
              minute: "2-digit",
            })}{" "}
            –{" "}
            {formatInNewYork(entry.ends_at, {
              hour: "2-digit",
              minute: "2-digit",
            })}
          </Typography>
        </Box>
      </CardContent>

      <CardActions sx={{ px: 2, pb: 2, gap: 1 }}>
        <Button
          variant="contained"
          color="success"
          onClick={handleAccept}
          disabled={isAccepting || isDeclining || isExpiredCountdown}
          startIcon={isAccepting ? <CircularProgress size={16} color="inherit" /> : null}
          aria-label="Accept waitlist offer"
        >
          {isAccepting ? "Accepting..." : "Accept Offer"}
        </Button>

        <Button
          variant="outlined"
          color="error"
          onClick={() => setShowDeclineConfirm(true)}
          disabled={isAccepting || isDeclining}
          aria-label="Decline waitlist offer"
        >
          Decline Offer
        </Button>
      </CardActions>

      {/* Decline Confirmation Dialog */}
      <Dialog
        open={showDeclineConfirm}
        onClose={() => setShowDeclineConfirm(false)}
        maxWidth="xs"
        fullWidth
        aria-labelledby="decline-dialog-title"
      >
        <DialogTitle id="decline-dialog-title" sx={{ fontWeight: "bold" }}>
          Decline Waitlist Offer
        </DialogTitle>
        <DialogContent>
          <DialogContentText>
            Are you sure you want to decline this reservation offer? Declining will permanently
            release this slot to the next person on the waitlist.
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setShowDeclineConfirm(false)} disabled={isDeclining} color="inherit">
            Keep Offer
          </Button>
          <Button
            onClick={handleDecline}
            variant="contained"
            color="error"
            disabled={isDeclining}
            startIcon={isDeclining ? <CircularProgress size={16} color="inherit" /> : null}
          >
            {isDeclining ? "Declining..." : "Confirm Decline"}
          </Button>
        </DialogActions>
      </Dialog>
    </Card>
  );
}
