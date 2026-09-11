import { useState, useEffect } from "react";
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
import {
  beginAttempt,
  restoreAttempt,
  clearAttempt,
  type CreateAttempt,
} from "../../api/createAttempt";
import { useAuth } from "../auth/AuthContext";
import { useQueryClient } from "@tanstack/react-query";
import { formatInNewYork } from "../resources/timeUtils";
import type { components } from "../../api/schema";

export type Resource = components["schemas"]["Resource"];
export type Booking = components["schemas"]["Booking"];
export type BookingCreate = components["schemas"]["BookingCreate"];

export interface BookingDialogProps {
  open: boolean;
  onClose: () => void;
  resource: Resource | null;
  window: {
    starts_at: string;
    ends_at: string;
  } | null;
  onBookingSuccess?: (booking: Booking) => void;
  onShowWaitlist?: (window: { starts_at: string; ends_at: string }) => void;
  onNavigateMyBookings?: () => void;
}

const MAX_ATTEMPT_AGE_MS = 24 * 60 * 60 * 1000; // 24 hours

export function BookingDialog({
  open,
  onClose,
  resource,
  window: bookingWindow,
  onBookingSuccess,
  onShowWaitlist,
  onNavigateMyBookings,
}: BookingDialogProps) {
  const { user } = useAuth();
  const queryClient = useQueryClient();

  const [isSubmitting, setIsSubmitting] = useState(false);
  const [currentAttempt, setCurrentAttempt] = useState<CreateAttempt | null>(null);
  const [isUncertain, setIsUncertain] = useState(false);
  const [isStaleDraft, setIsStaleDraft] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [slotConflict, setSlotConflict] = useState(false);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [showAbandonWarning, setShowAbandonWarning] = useState(false);

  // Restore attempt on mount or when dialog opens
  useEffect(() => {
    if (!open || !user) return;

    setErrorMessage(null);
    setFieldErrors({});
    setSlotConflict(false);
    setSuccessMessage(null);
    setShowAbandonWarning(false);

    const saved = restoreAttempt(user.id);
    if (saved && saved.kind === "booking") {
      const createdTime = new Date(saved.createdAt).getTime();
      const age = Date.now() - createdTime;

      if (!isNaN(createdTime) && age >= MAX_ATTEMPT_AGE_MS) {
        // Spec 7.3: At/over 24 hours, stop automatic replay and offer My Bookings check
        setIsStaleDraft(true);
        setCurrentAttempt(saved);
        setIsUncertain(false);
      } else {
        // Under 24 hours: same-key retry is available
        setIsStaleDraft(false);
        setCurrentAttempt(saved);
        setIsUncertain(true);
      }
    } else {
      setIsStaleDraft(false);
      setCurrentAttempt(null);
      setIsUncertain(false);
    }
  }, [open, user]);

  const parseFieldErrors = (details: Record<string, unknown>): Record<string, string> => {
    const res: Record<string, string> = {};
    if (!details) return res;

    // FastAPI/Pydantic envelope: { errors: [{ loc: ["body", "starts_at"], msg: "..." }] }
    if (Array.isArray(details.errors)) {
      for (const err of details.errors) {
        if (err && typeof err === "object") {
          const loc = Array.isArray(err.loc) ? err.loc.join(".") : String(err.loc || "field");
          res[loc] = typeof err.msg === "string" ? err.msg : JSON.stringify(err.msg);
        }
      }
    } else if (typeof details.detail === "string") {
      res["detail"] = details.detail;
    } else if (Array.isArray(details.detail)) {
      for (const err of details.detail) {
        if (err && typeof err === "object") {
          const loc = Array.isArray(err.loc) ? err.loc.join(".") : "field";
          res[loc] = typeof err.msg === "string" ? err.msg : JSON.stringify(err.msg);
        }
      }
    }
    return res;
  };

  const handleStartNewAttempt = () => {
    clearAttempt();
    setCurrentAttempt(null);
    setIsUncertain(false);
    setIsStaleDraft(false);
    setShowAbandonWarning(false);
    setErrorMessage(null);
    setFieldErrors({});
    setSlotConflict(false);
  };

  const handleSubmit = async () => {
    const storedPayload = currentAttempt?.payload as BookingCreate | undefined;
    const windowToUse = (isUncertain || isStaleDraft) && storedPayload ? storedPayload : bookingWindow;
    if (!user || !resource || !windowToUse) return;

    setIsSubmitting(true);
    setErrorMessage(null);
    setFieldErrors({});
    setSlotConflict(false);

    let attemptToUse: CreateAttempt;

    if ((isUncertain || isStaleDraft) && currentAttempt) {
      // While an uncertain attempt is active, replay exact same key and stored payload (Spec 4.3, 7.3, R4)
      attemptToUse = currentAttempt;
    } else {
      // New attempt with fresh UUID v4 Idempotency-Key for selected window
      const payload: BookingCreate = {
        resource_id: resource.id,
        starts_at: windowToUse.starts_at,
        ends_at: windowToUse.ends_at,
      };
      attemptToUse = beginAttempt(user.id, "booking", payload);
      setCurrentAttempt(attemptToUse);
    }

    try {
      const response = await request<Booking>("/api/v1/bookings", {
        method: "POST",
        headers: {
          "Idempotency-Key": attemptToUse.key,
          "Content-Type": "application/json",
        },
        body: attemptToUse.payload,
      });

      // Definitive 201 Created
      clearAttempt();
      setCurrentAttempt(null);
      setIsUncertain(false);

      // Invalidate queries per Spec 7.2
      queryClient.invalidateQueries({ queryKey: ["resources"] });
      queryClient.invalidateQueries({ queryKey: ["bookings", "mine"] });

      setSuccessMessage("Booking confirmed successfully!");
      if (onBookingSuccess) {
        onBookingSuccess(response.data);
      }

      setTimeout(() => {
        onClose();
      }, 1200);
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 409) {
          // Definitive 409 Conflict: completed outcome, clear attempt, require explicit new key
          clearAttempt();
          setCurrentAttempt(null);
          setIsUncertain(false);
          queryClient.invalidateQueries({ queryKey: ["resources"] });

          if (err.code === "SLOT_CONFLICT") {
            setSlotConflict(true);
            setErrorMessage(
              err.message ||
                "This slot has already been reserved. Please choose another time or join the waitlist."
            );
          } else if (err.code === "RESOURCE_INACTIVE") {
            setErrorMessage("This resource is currently inactive and cannot be reserved.");
          } else {
            setErrorMessage(err.message || "Conflict occurred while creating booking.");
          }
        } else if (err.status === 422) {
          // Definitive 422 Validation error: clear attempt, render readable errors
          clearAttempt();
          setCurrentAttempt(null);
          setIsUncertain(false);

          const parsed = parseFieldErrors(err.details);
          setFieldErrors(parsed);
          setErrorMessage(err.message || "Please correct the highlighted validation errors.");
        } else if (err.status === 503) {
          // Uncertain 503: retain attempt, do NOT rotate key, offer same-key retry
          setIsUncertain(true);
          setErrorMessage(
            err.message || "The booking service is temporarily unavailable. Please retry."
          );
        } else {
          // Other status (e.g. 500) treated as uncertain
          setIsUncertain(true);
          setErrorMessage(err.message || "An unexpected error occurred. You can retry.");
        }
      } else {
        // Network error / timeout: uncertain result, retain attempt and key
        setIsUncertain(true);
        setErrorMessage("Network error or timeout. Your previous request may still be processing.");
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleAbandonAttempt = () => {
    setShowAbandonWarning(true);
  };

  const handleConfirmAbandon = () => {
    handleStartNewAttempt();
  };

  // While an uncertain or stale attempt is active, displayed window matches the stored payload (Spec 7.3, R4).
  // A newly selected window may only be used after explicit abandonment.
  const storedDraftPayload = currentAttempt?.payload as BookingCreate | undefined;
  const effectiveWindow =
    (isUncertain || isStaleDraft) && storedDraftPayload ? storedDraftPayload : bookingWindow;

  return (
    <Dialog
      open={open}
      onClose={isSubmitting ? undefined : onClose}
      maxWidth="sm"
      fullWidth
      aria-labelledby="booking-dialog-title"
      aria-describedby="booking-dialog-description"
    >
      <DialogTitle id="booking-dialog-title" sx={{ fontWeight: "bold" }}>
        Confirm Reservation
      </DialogTitle>

      <DialogContent id="booking-dialog-description">


        {successMessage && (
          <Alert severity="success" role="status" sx={{ mb: 2 }}>
            {successMessage}
          </Alert>
        )}

        {/* 24-Hour Stale Gate Alert (Spec 7.3) */}
        {isStaleDraft && (
          <Alert severity="warning" role="alert" sx={{ mb: 2 }}>
            <Typography variant="body2" fontWeight="bold" gutterBottom>
              Previous Submission Pending
            </Typography>
            <Typography variant="body2" paragraph>
              A booking submission for this account from over 24 hours ago was not completed
              definitively. To prevent duplicate reservations, please check your bookings before
              starting a new reservation.
            </Typography>
            <Box sx={{ display: "flex", gap: 1, mt: 1 }}>
              {onNavigateMyBookings && (
                <Button
                  size="small"
                  variant="outlined"
                  color="warning"
                  onClick={() => {
                    onClose();
                    onNavigateMyBookings();
                  }}
                >
                  Check My Bookings
                </Button>
              )}
              <Button size="small" variant="contained" color="warning" onClick={handleStartNewAttempt}>
                Start New Attempt
              </Button>
            </Box>
          </Alert>
        )}

        {/* Uncertain Result / Retry Alert (Spec 7.3) */}
        {isUncertain && !isStaleDraft && (
          <Alert severity="info" role="alert" sx={{ mb: 2 }}>
            <Typography variant="body2" fontWeight="bold">
              Uncertain Request Recovery
            </Typography>
            <Typography variant="body2">
              The previous request encountered a timeout or temporary error. You can retry with the
              same idempotency key, or check your bookings.
            </Typography>
            {currentAttempt && (
              <Typography variant="caption" display="block" color="text.secondary" sx={{ mt: 0.5 }}>
                Key: {currentAttempt.key.slice(0, 8)}...
              </Typography>
            )}
          </Alert>
        )}

        {/* Abandonment warning */}
        {showAbandonWarning && (
          <Alert severity="warning" role="alert" sx={{ mb: 2 }}>
            <Typography variant="body2">
              Warning: If you abandon this attempt and change your reservation details, an earlier
              submission may have already succeeded on the server. We recommend checking your
              bookings first.
            </Typography>
            <Box sx={{ display: "flex", gap: 1, mt: 1 }}>
              {onNavigateMyBookings && (
                <Button
                  size="small"
                  variant="outlined"
                  onClick={() => {
                    onClose();
                    onNavigateMyBookings();
                  }}
                >
                  Check My Bookings
                </Button>
              )}
              <Button size="small" color="error" variant="contained" onClick={handleConfirmAbandon}>
                Abandon and Start Over
              </Button>
            </Box>
          </Alert>
        )}

        {errorMessage && !showAbandonWarning && (
          <Alert severity="error" role="alert" sx={{ mb: 2 }}>
            {errorMessage}
            {slotConflict && onShowWaitlist && effectiveWindow && (
              <Box sx={{ mt: 1.5 }}>
                <Button
                  size="small"
                  variant="contained"
                  color="primary"
                  onClick={() => {
                    onClose();
                    onShowWaitlist({
                      starts_at: effectiveWindow.starts_at,
                      ends_at: effectiveWindow.ends_at,
                    });
                  }}
                >
                  Join Waitlist for This Slot
                </Button>
              </Box>
            )}
          </Alert>
        )}

        {/* Field validation errors (Spec 4.1, 7.2) */}
        {Object.keys(fieldErrors).length > 0 && (
          <Box sx={{ mb: 2 }}>
            {Object.entries(fieldErrors).map(([field, msg]) => (
              <Typography key={field} variant="caption" color="error" display="block">
                • {field}: {msg}
              </Typography>
            ))}
          </Box>
        )}

        {/* Resource & Time Window Details */}
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

          {effectiveWindow && (
            <Box sx={{ my: 1 }}>
              <Typography variant="body2" color="text.secondary">
                Scheduled Slot (America/New_York):
              </Typography>
              <Typography variant="body1" fontWeight="medium">
                {formatInNewYork(effectiveWindow.starts_at, {
                  weekday: "short",
                  month: "short",
                  day: "numeric",
                  year: "numeric",
                })}
              </Typography>
              <Typography variant="body2" fontWeight="bold" color="primary.main">
                {formatInNewYork(effectiveWindow.starts_at, {
                  hour: "2-digit",
                  minute: "2-digit",
                })}{" "}
                –{" "}
                {formatInNewYork(effectiveWindow.ends_at, {
                  hour: "2-digit",
                  minute: "2-digit",
                })}
              </Typography>
              <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 0.5 }}>
                UTC: {effectiveWindow.starts_at} to {effectiveWindow.ends_at}
              </Typography>
            </Box>
          )}
        </Box>
      </DialogContent>

      <DialogActions sx={{ px: 3, pb: 2 }}>
        {isUncertain && !isStaleDraft && !showAbandonWarning && (
          <Button
            onClick={handleAbandonAttempt}
            color="inherit"
            disabled={isSubmitting}
            size="small"
          >
            Abandon Attempt
          </Button>
        )}

        <Button onClick={onClose} disabled={isSubmitting} color="inherit">
          Cancel
        </Button>

        {!isStaleDraft && (
          <Button
            onClick={handleSubmit}
            variant="contained"
            color="primary"
            disabled={isSubmitting || !!successMessage}
            startIcon={isSubmitting ? <CircularProgress size={16} color="inherit" /> : null}
          >
            {isSubmitting
              ? "Submitting..."
              : isUncertain
              ? "Retry Booking"
              : "Confirm Reservation"}
          </Button>
        )}
      </DialogActions>
    </Dialog>
  );
}
