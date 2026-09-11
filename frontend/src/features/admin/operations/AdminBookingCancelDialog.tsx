import { useState } from "react";
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogContentText,
  DialogActions,
  Button,
  TextField,
  Alert,
  CircularProgress,
} from "@mui/material";
import { useQueryClient } from "@tanstack/react-query";
import { request, ApiError } from "../../../api/client";
import type { components } from "../../../api/schema";

type Booking = components["schemas"]["Booking"];

interface AdminBookingCancelDialogProps {
  open: boolean;
  booking: Booking | null;
  onClose: () => void;
  onCancelled?: () => void;
}

export function AdminBookingCancelDialog({
  open,
  booking,
  onClose,
  onCancelled,
}: AdminBookingCancelDialogProps) {
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [versionMismatchAlert, setVersionMismatchAlert] = useState<string | null>(null);

  const handleClose = () => {
    if (isSubmitting) return;
    setReason("");
    setErrorMsg(null);
    setVersionMismatchAlert(null);
    onClose();
  };

  const handleConfirmCancel = async () => {
    if (!booking) return;

    setIsSubmitting(true);
    setErrorMsg(null);
    setVersionMismatchAlert(null);

    try {
      // 1. Fetch latest booking ETag (Spec 4.1, 7.2)
      let etag = String(booking.version);
      try {
        const detailRes = await request<Booking>(`/api/v1/admin/bookings/${booking.id}`);
        etag = detailRes.etag || String(detailRes.data.version);
      } catch {
        // fallback to current version
      }

      // 2. Perform cancel with If-Match (Spec 4.2 E26)
      await request<Booking>(`/api/v1/admin/bookings/${booking.id}/cancel`, {
        method: "POST",
        headers: {
          "If-Match": `"${etag}"`,
          "Content-Type": "application/json",
        },
        body: {
          reason: reason.trim(),
        },
      });

      queryClient.invalidateQueries({ queryKey: ["admin", "bookings"] });
      queryClient.invalidateQueries({ queryKey: ["resources"] });

      if (onCancelled) {
        onCancelled();
      }
      handleClose();
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 412) {
          setVersionMismatchAlert(
            "Another update occurred to this booking. Refreshing latest status..."
          );
          queryClient.invalidateQueries({ queryKey: ["admin", "bookings"] });
        } else if (err.status === 409) {
          if (err.code === "TOO_LATE") {
            setErrorMsg(
              "This reservation can no longer be cancelled because the cancellation deadline has passed."
            );
          } else if (err.code === "INVALID_STATE") {
            setErrorMsg(
              "This reservation is in an invalid or terminal state for cancellation."
            );
          } else {
            setErrorMsg(err.message || "Cannot cancel booking due to conflict.");
          }
        } else {
          setErrorMsg(err.message || "Failed to cancel booking.");
        }
      } else {
        setErrorMsg("A network or unexpected error occurred.");
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={handleClose}
      aria-labelledby="cancel-admin-booking-dialog-title"
      maxWidth="sm"
      fullWidth
    >
      <DialogTitle id="cancel-admin-booking-dialog-title">
        Cancel Reservation (Administrator)
      </DialogTitle>
      <DialogContent>
        {versionMismatchAlert && (
          <Alert severity="warning" sx={{ mb: 2 }} role="alert">
            {versionMismatchAlert}
          </Alert>
        )}

        {errorMsg && (
          <Alert severity="error" sx={{ mb: 2 }} role="alert">
            {errorMsg}
          </Alert>
        )}

        <DialogContentText sx={{ mb: 2 }}>
          Are you sure you want to cancel reservation <code>{booking?.id}</code>?
        </DialogContentText>

        <TextField
          id="admin-cancel-reason"
          label="Cancellation Reason (Optional)"
          multiline
          rows={2}
          fullWidth
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          inputProps={{ maxLength: 500 }}
          helperText="Max 500 characters"
          disabled={isSubmitting}
        />
      </DialogContent>
      <DialogActions>
        <Button onClick={handleClose} disabled={isSubmitting} color="inherit">
          Close
        </Button>
        <Button
          onClick={handleConfirmCancel}
          variant="contained"
          color="error"
          disabled={isSubmitting}
          startIcon={isSubmitting ? <CircularProgress size={18} /> : null}
        >
          {isSubmitting ? "Cancelling..." : "Confirm Cancellation"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
