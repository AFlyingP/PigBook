import React, { useState, useEffect } from "react";
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  TextField,
  FormControlLabel,
  Switch,
  Alert,
  Box,
  CircularProgress,
  Typography,
} from "@mui/material";
import { useQueryClient } from "@tanstack/react-query";
import { request, ApiError } from "../../../api/client";
import type { components } from "../../../api/schema";

type Resource = components["schemas"]["Resource"];
type ResourcePatch = components["schemas"]["ResourcePatch"];

interface ResourceEditDialogProps {
  open: boolean;
  resource: Resource | null;
  onClose: () => void;
  onUpdated?: (resource: Resource) => void;
}

export function ResourceEditDialog({
  open,
  resource,
  onClose,
  onUpdated,
}: ResourceEditDialogProps) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [location, setLocation] = useState("");
  const [description, setDescription] = useState("");
  const [active, setActive] = useState(true);
  const [currentVersion, setCurrentVersion] = useState<number>(1);
  const [currentEtag, setCurrentEtag] = useState<string | undefined>(undefined);

  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isRefetching, setIsRefetching] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [versionMismatchAlert, setVersionMismatchAlert] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<{ [k: string]: string }>({});

  useEffect(() => {
    if (resource && open) {
      setName(resource.name);
      setLocation(resource.location);
      setDescription(resource.description || "");
      setActive(resource.active);
      setCurrentVersion(resource.version);
      setCurrentEtag(String(resource.version));
      setErrorMsg(null);
      setVersionMismatchAlert(null);
      setFieldErrors({});
    }
  }, [resource, open]);

  const handleClose = () => {
    if (isSubmitting || isRefetching) return;
    setErrorMsg(null);
    setVersionMismatchAlert(null);
    onClose();
  };

  const validate = () => {
    const errs: { [k: string]: string } = {};
    if (!name.trim()) {
      errs.name = "Name is required (1-100 characters)";
    } else if (name.length > 100) {
      errs.name = "Name must not exceed 100 characters";
    }

    if (!location.trim()) {
      errs.location = "Location is required (1-200 characters)";
    } else if (location.length > 200) {
      errs.location = "Location must not exceed 200 characters";
    }

    if (description.length > 2000) {
      errs.description = "Description must not exceed 2000 characters";
    }

    setFieldErrors(errs);
    return Object.keys(errs).length === 0;
  };

  const refetchLatest = async () => {
    if (!resource) return;
    setIsRefetching(true);
    try {
      const res = await request<Resource>(`/api/v1/resources/${resource.id}`);
      setName(res.data.name);
      setLocation(res.data.location);
      setDescription(res.data.description || "");
      setActive(res.data.active);
      setCurrentVersion(res.data.version);
      setCurrentEtag(res.etag || String(res.data.version));
    } catch {
      // If active=false or admin route needed
      try {
        const adminRes = await request<{ items: Resource[] }>(
          `/api/v1/admin/resources?limit=100`
        );
        const match = adminRes.data.items.find((r) => r.id === resource.id);
        if (match) {
          setName(match.name);
          setLocation(match.location);
          setDescription(match.description || "");
          setActive(match.active);
          setCurrentVersion(match.version);
          setCurrentEtag(String(match.version));
        }
      } catch {
        // fallback
      }
    } finally {
      setIsRefetching(false);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!resource || !validate()) return;

    setIsSubmitting(true);
    setErrorMsg(null);
    setVersionMismatchAlert(null);

    const payload: ResourcePatch = {
      name: name.trim(),
      location: location.trim(),
      description: description.trim(),
      active,
    };

    const ifMatchValue = currentEtag ? `"${currentEtag}"` : `"${currentVersion}"`;

    try {
      const res = await request<Resource>(`/api/v1/admin/resources/${resource.id}`, {
        method: "PATCH",
        headers: {
          "If-Match": ifMatchValue,
          "Content-Type": "application/json",
        },
        body: payload,
      });

      queryClient.invalidateQueries({ queryKey: ["admin", "resources"] });
      queryClient.invalidateQueries({ queryKey: ["resources"] });

      if (onUpdated) {
        onUpdated(res.data);
      }
      handleClose();
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 412) {
          // Spec 7.2: 412 refetches detail and tells user another update occurred; never overwrite silently.
          setVersionMismatchAlert(
            "Another update occurred to this resource. Reloading latest details... Please review the changes before saving."
          );
          await refetchLatest();
        } else if (err.status === 409) {
          if (err.code === "RESOURCE_IN_USE") {
            setErrorMsg(
              "Cannot modify this resource: it currently has future confirmed or offered bookings, or waitlist entries."
            );
          } else {
            setErrorMsg(err.message || "Conflict updating resource.");
          }
        } else {
          setErrorMsg(err.message || "Failed to update resource.");
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
      aria-labelledby="edit-resource-dialog-title"
      maxWidth="sm"
      fullWidth
    >
      <form onSubmit={handleSubmit} noValidate>
        <DialogTitle id="edit-resource-dialog-title">
          Edit Resource — {resource?.name}
        </DialogTitle>
        <DialogContent dividers>
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

          {isRefetching && (
            <Box sx={{ display: "flex", alignItems: "center", gap: 1, mb: 2 }}>
              <CircularProgress size={16} />
              <Typography variant="body2" color="text.secondary">
                Fetching latest version...
              </Typography>
            </Box>
          )}

          <Box sx={{ display: "flex", flexDirection: "column", gap: 2, pt: 1 }}>
            <TextField
              id="resource-edit-name"
              label="Resource Name"
              required
              fullWidth
              value={name}
              onChange={(e) => setName(e.target.value)}
              error={!!fieldErrors.name}
              helperText={fieldErrors.name || "Required, 1-100 characters"}
              inputProps={{ "aria-required": "true", maxLength: 100 }}
              disabled={isSubmitting || isRefetching}
            />

            <TextField
              id="resource-edit-location"
              label="Location"
              required
              fullWidth
              value={location}
              onChange={(e) => setLocation(e.target.value)}
              error={!!fieldErrors.location}
              helperText={fieldErrors.location || "Required, 1-200 characters"}
              inputProps={{ "aria-required": "true", maxLength: 200 }}
              disabled={isSubmitting || isRefetching}
            />

            <TextField
              id="resource-edit-description"
              label="Description"
              multiline
              rows={3}
              fullWidth
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              error={!!fieldErrors.description}
              helperText={fieldErrors.description || "Optional, up to 2000 characters"}
              inputProps={{ maxLength: 2000 }}
              disabled={isSubmitting || isRefetching}
            />

            <FormControlLabel
              control={
                <Switch
                  id="resource-edit-active"
                  checked={active}
                  onChange={(e) => setActive(e.target.checked)}
                  color="primary"
                  disabled={isSubmitting || isRefetching}
                />
              }
              label={
                <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
                  <span>Active for Reservations</span>
                  <Typography variant="caption" color="text.secondary">
                    ({active ? "Active" : "Archived / Inactive"})
                  </Typography>
                </Box>
              }
            />
          </Box>
        </DialogContent>
        <DialogActions>
          <Button onClick={handleClose} disabled={isSubmitting || isRefetching} color="inherit">
            Cancel
          </Button>
          <Button
            type="submit"
            variant="contained"
            color="primary"
            disabled={isSubmitting || isRefetching}
            startIcon={isSubmitting ? <CircularProgress size={18} /> : null}
          >
            {isSubmitting ? "Saving..." : "Save Changes"}
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}
