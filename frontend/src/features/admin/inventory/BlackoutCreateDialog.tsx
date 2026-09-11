import React, { useState, useEffect } from "react";
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  TextField,
  Alert,
  Box,
  CircularProgress,
} from "@mui/material";
import { useQueryClient } from "@tanstack/react-query";
import { request, ApiError } from "../../../api/client";
import {
  beginAttempt,
  restoreAttempt,
  clearAttempt,
  type CreateAttempt,
} from "../../../api/createAttempt";
import { useAuth } from "../../auth/AuthContext";
import type { components } from "../../../api/schema";

type Booking = components["schemas"]["Booking"];
type BlackoutCreate = components["schemas"]["BlackoutCreate"];

interface BlackoutCreateDialogProps {
  open: boolean;
  resourceId: string;
  resourceName: string;
  onClose: () => void;
  onCreated?: (booking: Booking) => void;
}

export function BlackoutCreateDialog({
  open,
  resourceId,
  resourceName,
  onClose,
  onCreated,
}: BlackoutCreateDialogProps) {
  const queryClient = useQueryClient();
  const { user } = useAuth();

  const [startsAt, setStartsAt] = useState("");
  const [endsAt, setEndsAt] = useState("");
  const [activeAttempt, setActiveAttempt] = useState<CreateAttempt | null>(null);

  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [conflictMsg, setConflictMsg] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<{ [k: string]: string }>({});
  const [isUncertain, setIsUncertain] = useState(false);
  const [isExpiredAttempt, setIsExpiredAttempt] = useState(false);

  // Restore existing uncertain attempt for this principal on mount/open
  useEffect(() => {
    if (open && user) {
      const stored = restoreAttempt(user.id);
      if (stored && stored.kind === "blackout") {
        const payload = stored.payload as {
          starts_at?: string;
          ends_at?: string;
          resource_id?: string;
        };
        if (!payload.resource_id || payload.resource_id === resourceId) {
          const createdTime = new Date(stored.createdAt).getTime();
          const ageHours = (Date.now() - createdTime) / (1000 * 60 * 60);

          setActiveAttempt(stored);
          if (payload.starts_at) setStartsAt(toInputFormat(payload.starts_at));
          if (payload.ends_at) setEndsAt(toInputFormat(payload.ends_at));

          if (ageHours >= 24) {
            setIsExpiredAttempt(true);
            setIsUncertain(true);
          } else {
            setIsUncertain(true);
          }
        }
      } else {
        // Default starts_at to tomorrow 09:00, ends_at to tomorrow 17:00
        const tomorrow = new Date();
        tomorrow.setDate(tomorrow.getDate() + 1);
        const yyyy = tomorrow.getFullYear();
        const mm = String(tomorrow.getMonth() + 1).padStart(2, "0");
        const dd = String(tomorrow.getDate()).padStart(2, "0");
        setStartsAt(`${yyyy}-${mm}-${dd}T09:00`);
        setEndsAt(`${yyyy}-${mm}-${dd}T17:00`);
      }
    }
  }, [open, user, resourceId]);

  function toInputFormat(isoStr: string): string {
    try {
      const d = new Date(isoStr);
      const yyyy = d.getFullYear();
      const mm = String(d.getMonth() + 1).padStart(2, "0");
      const dd = String(d.getDate()).padStart(2, "0");
      const hh = String(d.getHours()).padStart(2, "0");
      const min = String(d.getMinutes()).padStart(2, "0");
      return `${yyyy}-${mm}-${dd}T${hh}:${min}`;
    } catch {
      return isoStr;
    }
  }

  const handleClose = () => {
    if (isSubmitting) return;
    setErrorMsg(null);
    setConflictMsg(null);
    setFieldErrors({});
    setIsUncertain(false);
    setIsExpiredAttempt(false);
    setActiveAttempt(null);
    onClose();
  };

  const handleAbandon = () => {
    clearAttempt();
    setActiveAttempt(null);
    setIsUncertain(false);
    setIsExpiredAttempt(false);
    handleClose();
  };

  const validate = () => {
    const errs: { [k: string]: string } = {};
    if (!startsAt) {
      errs.startsAt = "Start time is required";
    }
    if (!endsAt) {
      errs.endsAt = "End time is required";
    }
    if (startsAt && endsAt) {
      const s = new Date(startsAt).getTime();
      const e = new Date(endsAt).getTime();
      if (isNaN(s)) {
        errs.startsAt = "Invalid start time";
      }
      if (isNaN(e)) {
        errs.endsAt = "Invalid end time";
      }
      if (!isNaN(s) && !isNaN(e) && e <= s) {
        errs.endsAt = "End time must be strictly after start time";
      }
    }
    setFieldErrors(errs);
    return Object.keys(errs).length === 0;
  };

  const handleSubmit = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    if (!validate()) return;
    if (!user) return;

    setIsSubmitting(true);
    setErrorMsg(null);
    setConflictMsg(null);

    const startIso = new Date(startsAt).toISOString();
    const endIso = new Date(endsAt).toISOString();

    let attempt = activeAttempt;
    if (!attempt) {
      attempt = beginAttempt(user.id, "blackout", {
        starts_at: startIso,
        ends_at: endIso,
        resource_id: resourceId,
      });
      setActiveAttempt(attempt);
    }

    const payload: BlackoutCreate = {
      starts_at: startIso,
      ends_at: endIso,
    };

    try {
      const res = await request<Booking>(
        `/api/v1/admin/resources/${resourceId}/blackouts`,
        {
          method: "POST",
          headers: {
            "Idempotency-Key": attempt.key,
            "Content-Type": "application/json",
          },
          body: payload,
        }
      );

      // Definitive 201: clear stored attempt
      clearAttempt();
      setActiveAttempt(null);
      setIsUncertain(false);

      queryClient.invalidateQueries({
        queryKey: ["admin", "resources", resourceId, "blackouts"],
      });
      queryClient.invalidateQueries({ queryKey: ["resources"] });

      if (onCreated) {
        onCreated(res.data);
      }
      handleClose();
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 409) {
          // Definitive 409: completed result! Clear attempt, show conflict, require new key
          clearAttempt();
          setActiveAttempt(null);
          setIsUncertain(false);

          if (err.code === "SLOT_CONFLICT") {
            setConflictMsg(
              "Schedule conflict: a reservation or blackout already occupies part of this window."
            );
          } else if (err.code === "RESOURCE_INACTIVE") {
            setConflictMsg("This resource is inactive and cannot receive new blackouts.");
          } else {
            setConflictMsg(err.message || "Conflict creating blackout.");
          }
          queryClient.invalidateQueries({
            queryKey: ["admin", "resources", resourceId, "blackouts"],
          });
        } else if (err.status === 422) {
          // Definitive 422: validation failure, clear attempt
          clearAttempt();
          setActiveAttempt(null);
          setIsUncertain(false);
          setErrorMsg(err.message || "Validation error creating blackout.");
        } else if (err.status === 503) {
          // Uncertain result (503): keep attempt, offer retry with same key
          setIsUncertain(true);
          setErrorMsg(
            "Service temporarily unavailable (503). The attempt key has been preserved. You may retry."
          );
        } else {
          setErrorMsg(err.message || "Failed to create blackout.");
        }
      } else {
        // Network error / timeout: uncertain result, keep attempt
        setIsUncertain(true);
        setErrorMsg(
          "Network interruption or timeout. The attempt key has been preserved. You may retry."
        );
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={handleClose}
      aria-labelledby="create-blackout-dialog-title"
      maxWidth="sm"
      fullWidth
    >
      <form onSubmit={handleSubmit} noValidate>
        <DialogTitle id="create-blackout-dialog-title">
          Create Blackout — {resourceName}
        </DialogTitle>
        <DialogContent dividers>
          {isUncertain && (
            <Alert severity="warning" sx={{ mb: 2 }} role="alert">
              {isExpiredAttempt ? (
                <span>
                  A previous attempt is older than 24 hours. Automatic replay has stopped.
                  Please check the blackout list to see if it was committed before starting a new attempt.
                </span>
              ) : (
                <span>
                  A previous attempt did not receive a definitive response. You can retry with the same
                  idempotency key or abandon this attempt.
                </span>
              )}
            </Alert>
          )}

          {conflictMsg && (
            <Alert severity="error" sx={{ mb: 2 }} role="alert">
              {conflictMsg}
            </Alert>
          )}

          {errorMsg && (
            <Alert severity="error" sx={{ mb: 2 }} role="alert">
              {errorMsg}
            </Alert>
          )}

          <Box sx={{ display: "flex", flexDirection: "column", gap: 2, pt: 1 }}>
            <TextField
              id="blackout-starts-at"
              label="Starts At"
              type="datetime-local"
              required
              fullWidth
              value={startsAt}
              onChange={(e) => {
                setStartsAt(e.target.value);
                if (isUncertain) {
                  // editing payload invalidates the old attempt
                  clearAttempt();
                  setActiveAttempt(null);
                  setIsUncertain(false);
                }
              }}
              error={!!fieldErrors.startsAt}
              helperText={fieldErrors.startsAt || "Local facility time"}
              InputLabelProps={{ shrink: true }}
              inputProps={{ "aria-required": "true" }}
              disabled={isSubmitting}
            />

            <TextField
              id="blackout-ends-at"
              label="Ends At"
              type="datetime-local"
              required
              fullWidth
              value={endsAt}
              onChange={(e) => {
                setEndsAt(e.target.value);
                if (isUncertain) {
                  clearAttempt();
                  setActiveAttempt(null);
                  setIsUncertain(false);
                }
              }}
              error={!!fieldErrors.endsAt}
              helperText={fieldErrors.endsAt || "Local facility time"}
              InputLabelProps={{ shrink: true }}
              inputProps={{ "aria-required": "true" }}
              disabled={isSubmitting}
            />
          </Box>
        </DialogContent>
        <DialogActions>
          {isUncertain ? (
            <>
              <Button onClick={handleAbandon} color="inherit" disabled={isSubmitting}>
                Abandon Attempt
              </Button>
              {!isExpiredAttempt && (
                <Button
                  onClick={() => handleSubmit()}
                  variant="contained"
                  color="warning"
                  disabled={isSubmitting}
                  startIcon={isSubmitting ? <CircularProgress size={18} /> : null}
                >
                  {isSubmitting ? "Retrying..." : "Retry with Same Key"}
                </Button>
              )}
            </>
          ) : (
            <>
              <Button onClick={handleClose} disabled={isSubmitting} color="inherit">
                Cancel
              </Button>
              <Button
                type="submit"
                variant="contained"
                color="primary"
                disabled={isSubmitting}
                startIcon={isSubmitting ? <CircularProgress size={18} /> : null}
              >
                {isSubmitting ? "Creating..." : "Create Blackout"}
              </Button>
            </>
          )}
        </DialogActions>
      </form>
    </Dialog>
  );
}
