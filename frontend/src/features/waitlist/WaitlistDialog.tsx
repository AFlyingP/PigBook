import { useState } from "react";
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  Typography,
  Alert,
  Box,
  CircularProgress,
  Divider,
} from "@mui/material";
import { request, ApiError } from "../../api/client";
import { useQueryClient } from "@tanstack/react-query";
import { formatInNewYork } from "../resources/timeUtils";
import type { components } from "../../api/schema";

export type Resource = components["schemas"]["Resource"];
export type WaitEntry = components["schemas"]["WaitEntry"];
export type WaitCreate = components["schemas"]["WaitCreate"];

export interface WaitlistDialogProps {
  open: boolean;
  onClose: () => void;
  resource: Resource | null;
  window: {
    starts_at: string;
    ends_at: string;
  } | null;
  onJoinSuccess?: (entry: WaitEntry) => void;
  onShowBooking?: (window: { starts_at: string; ends_at: string }) => void;
}

export function WaitlistDialog({
  open,
  onClose,
  resource,
  window: waitlistWindow,
  onJoinSuccess,
  onShowBooking,
}: WaitlistDialogProps) {
  const queryClient = useQueryClient();
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [slotAvailable, setSlotAvailable] = useState(false);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);

  const handleSubmit = async () => {
    if (!resource || !waitlistWindow) return;

    setIsSubmitting(true);
    setErrorMessage(null);
    setSlotAvailable(false);
    setSuccessMessage(null);

    try {
      const payload: WaitCreate = {
        resource_id: resource.id,
        starts_at: waitlistWindow.starts_at,
        ends_at: waitlistWindow.ends_at,
      };

      const res = await request<WaitEntry>("/api/v1/waitlist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: payload,
      });

      // Invalidate queries per Spec 7.2
      queryClient.invalidateQueries({ queryKey: ["waitlist", "mine"] });
      queryClient.invalidateQueries({ queryKey: ["resources"] });

      setSuccessMessage("You have joined the waitlist for this slot!");
      if (onJoinSuccess) {
        onJoinSuccess(res.data);
      }

      setTimeout(() => {
        onClose();
      }, 1200);
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 409) {
          if (err.code === "SLOT_AVAILABLE") {
            // Interval became free: refresh availability and offer booking (Spec 7.1, 7.2)
            setSlotAvailable(true);
            queryClient.invalidateQueries({ queryKey: ["resources"] });
            setErrorMessage(
              "Good news! This slot is currently available for direct booking."
            );
          } else if (err.code === "WAITLIST_FULL") {
            // Capacity message with no claim of having joined (Spec 7.3)
            setErrorMessage(
              "The waitlist for this resource is currently full (capacity reached). Please check back later."
            );
          } else if (err.code === "ALREADY_WAITLISTED") {
            setErrorMessage("You are already on the waitlist for this time slot.");
          } else if (err.code === "ALREADY_BOOKED") {
            setErrorMessage("You already hold an active booking during this interval.");
          } else if (err.code === "RESOURCE_INACTIVE") {
            setErrorMessage("This resource is inactive and does not accept waitlist entries.");
          } else {
            setErrorMessage(err.message || "Unable to join the waitlist at this time.");
          }
        } else if (err.status === 422) {
          setErrorMessage(err.message || "Invalid time window requested for waitlist.");
        } else {
          setErrorMessage(err.message || "An error occurred while joining the waitlist.");
        }
      } else {
        setErrorMessage("Network error while connecting to the waitlist service.");
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={isSubmitting ? undefined : onClose}
      maxWidth="sm"
      fullWidth
      aria-labelledby="waitlist-dialog-title"
    >
      <DialogTitle id="waitlist-dialog-title" sx={{ fontWeight: "bold" }}>
        Join Waitlist
      </DialogTitle>

      <DialogContent>
        {successMessage && (
          <Alert severity="success" role="status" sx={{ mb: 2 }}>
            {successMessage}
          </Alert>
        )}

        {errorMessage && (
          <Alert severity={slotAvailable ? "info" : "error"} role="alert" sx={{ mb: 2 }}>
            {errorMessage}
            {slotAvailable && onShowBooking && waitlistWindow && (
              <Box sx={{ mt: 1.5 }}>
                <Button
                  size="small"
                  variant="contained"
                  color="primary"
                  onClick={() => {
                    onClose();
                    onShowBooking(waitlistWindow);
                  }}
                >
                  Book This Slot Now
                </Button>
              </Box>
            )}
          </Alert>
        )}

        <Box sx={{ py: 1 }}>
          <Typography variant="subtitle1" fontWeight="bold">
            {resource?.name || "Resource"}
          </Typography>
          {resource?.location && (
            <Typography variant="body2" color="text.secondary" gutterBottom>
              📍 {resource.location}
            </Typography>
          )}

          <Divider sx={{ my: 1.5 }} />

          {waitlistWindow && (
            <Box sx={{ my: 1 }}>
              <Typography variant="body2" color="text.secondary">
                Desired Time Window (America/New_York):
              </Typography>
              <Typography variant="body1" fontWeight="medium">
                {formatInNewYork(waitlistWindow.starts_at, {
                  weekday: "short",
                  month: "short",
                  day: "numeric",
                  year: "numeric",
                })}
              </Typography>
              <Typography variant="body2" fontWeight="bold" color="primary.main">
                {formatInNewYork(waitlistWindow.starts_at, {
                  hour: "2-digit",
                  minute: "2-digit",
                })}{" "}
                –{" "}
                {formatInNewYork(waitlistWindow.ends_at, {
                  hour: "2-digit",
                  minute: "2-digit",
                })}
              </Typography>
              <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 0.5 }}>
                UTC: {waitlistWindow.starts_at} to {waitlistWindow.ends_at}
              </Typography>
            </Box>
          )}

          <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>
            If this slot becomes available through a cancellation, you will receive an offer with
            a 15-minute countdown window to confirm your booking.
          </Typography>
        </Box>
      </DialogContent>

      <DialogActions sx={{ px: 3, pb: 2 }}>
        <Button onClick={onClose} disabled={isSubmitting} color="inherit">
          Cancel
        </Button>
        <Button
          onClick={handleSubmit}
          variant="contained"
          color="primary"
          disabled={isSubmitting || !!successMessage}
          startIcon={isSubmitting ? <CircularProgress size={16} color="inherit" /> : null}
        >
          {isSubmitting ? "Joining..." : "Join Waitlist"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
