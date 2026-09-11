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

type Resource = components["schemas"]["Resource"];

interface ResourceArchiveDialogProps {
  open: boolean;
  resource: Resource | null;
  onClose: () => void;
  onArchived?: () => void;
}

export function ResourceArchiveDialog({
  open,
  resource,
  onClose,
  onArchived,
}: ResourceArchiveDialogProps) {
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

  const handleConfirmArchive = async () => {
    if (!resource) return;

    setIsSubmitting(true);
    setErrorMsg(null);
    setVersionMismatchAlert(null);

    const ifMatchValue = `"${resource.version}"`;

    try {
      await request<Resource>(`/api/v1/admin/resources/${resource.id}`, {
        method: "DELETE",
        headers: {
          "If-Match": ifMatchValue,
        },
      });

      queryClient.invalidateQueries({ queryKey: ["admin", "resources"] });
      queryClient.invalidateQueries({ queryKey: ["resources"] });

      if (onArchived) {
        onArchived();
      }
      handleClose();
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 412) {
          setVersionMismatchAlert(
            "Another update occurred to this resource. The latest state has been refreshed; please review before archiving."
          );
          queryClient.invalidateQueries({ queryKey: ["admin", "resources"] });
        } else if (err.status === 409) {
          if (err.code === "RESOURCE_IN_USE") {
            setErrorMsg(
              "Cannot archive this resource: it currently has future confirmed reservations, active offers, or waitlist entries (RESOURCE_IN_USE)."
            );
          } else {
            setErrorMsg(err.message || "Cannot archive resource due to conflict.");
          }
        } else {
          setErrorMsg(err.message || "Failed to archive resource.");
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
      aria-labelledby="archive-resource-dialog-title"
      maxWidth="xs"
      fullWidth
    >
      <DialogTitle id="archive-resource-dialog-title">Archive Resource</DialogTitle>
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
          Are you sure you want to archive <strong>{resource?.name}</strong>?
        </DialogContentText>
        <DialogContentText variant="body2" sx={{ mt: 1 }} color="text.secondary">
          This is a soft archive that marks the resource inactive for new reservations.
          Past bookings, audit history, and associated records are preserved.
        </DialogContentText>
      </DialogContent>
      <DialogActions>
        <Button onClick={handleClose} disabled={isSubmitting} color="inherit">
          Cancel
        </Button>
        <Button
          onClick={handleConfirmArchive}
          variant="contained"
          color="warning"
          disabled={isSubmitting}
          startIcon={isSubmitting ? <CircularProgress size={18} /> : null}
        >
          {isSubmitting ? "Archiving..." : "Archive Resource"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
