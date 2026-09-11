import { useState } from "react";
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogContentText,
  DialogActions,
  Button,
  Alert,
  CircularProgress,
} from "@mui/material";
import { useQueryClient } from "@tanstack/react-query";
import { request, ApiError } from "../../../api/client";
import type { components } from "../../../api/schema";

type Booking = components["schemas"]["Booking"];

interface BlackoutCancelDialogProps {
  open: boolean;
  blackout: Booking | null;
  resourceId: string;
  onClose: () => void;
  onCancelled?: () => void;
}

export function BlackoutCancelDialog({
  open,
  blackout,
  resourceId,
  onClose,
  onCancelled,
}: BlackoutCancelDialogProps) {
  const queryClient = useQueryClient();
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [versionMismatchAlert, setVersionMismatchAlert] = useState<string | null>(null);

  const handleClose = () => {
    if (isSubmitting) return;
    setErrorMsg(null);
    setVersionMismatchAlert(null);
    onClose();
  };

  const handleConfirmCancel = async () => {
    if (!blackout) return;

    setIsSubmitting(true);
    setErrorMsg(null);
    setVersionMismatchAlert(null);

    const ifMatchValue = `"${blackout.version}"`;

    try {
      await request<Booking>(`/api/v1/admin/blackouts/${blackout.id}`, {
        method: "DELETE",
        headers: {
          "If-Match": ifMatchValue,
        },
      });

      queryClient.invalidateQueries({
        queryKey: ["admin", "resources", resourceId, "blackouts"],
      });
      queryClient.invalidateQueries({ queryKey: ["resources"] });

      if (onCancelled) {
        onCancelled();
      }
      handleClose();
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 412) {
          setVersionMismatchAlert(
            "Another update occurred to this blackout. Refreshing latest status..."
          );
          queryClient.invalidateQueries({
            queryKey: ["admin", "resources", resourceId, "blackouts"],
          });
        } else if (err.status === 409) {
          if (err.code === "TOO_LATE") {
            setErrorMsg("This blackout has already started or passed and cannot be cancelled.");
          } else {
            setErrorMsg(err.message || "Conflict cancelling blackout.");
          }
        } else {
          setErrorMsg(err.message || "Failed to cancel blackout.");
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
      aria-labelledby="cancel-blackout-dialog-title"
      maxWidth="xs"
      fullWidth
    >
      <DialogTitle id="cancel-blackout-dialog-title">Cancel Blackout</DialogTitle>
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

        <DialogContentText>
          Are you sure you want to cancel this blackout window?
        </DialogContentText>
        {blackout && (
          <DialogContentText variant="body2" sx={{ mt: 1 }} color="text.secondary">
            From: {new Date(blackout.starts_at).toLocaleString()}
            <br />
            To: {new Date(blackout.ends_at).toLocaleString()}
          </DialogContentText>
        )}
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
