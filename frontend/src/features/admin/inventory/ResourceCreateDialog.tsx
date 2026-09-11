import React, { useState } from "react";
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
import type { components } from "../../../api/schema";

type Resource = components["schemas"]["Resource"];
type ResourceCreate = components["schemas"]["ResourceCreate"];

interface ResourceCreateDialogProps {
  open: boolean;
  onClose: () => void;
  onCreated?: (resource: Resource) => void;
}

export function ResourceCreateDialog({
  open,
  onClose,
  onCreated,
}: ResourceCreateDialogProps) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [location, setLocation] = useState("");
  const [description, setDescription] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<{ [k: string]: string }>({});

  const handleClose = () => {
    if (isSubmitting) return;
    setName("");
    setLocation("");
    setDescription("");
    setErrorMsg(null);
    setFieldErrors({});
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

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!validate()) return;

    setIsSubmitting(true);
    setErrorMsg(null);

    const payload: ResourceCreate = {
      name: name.trim(),
      location: location.trim(),
      description: description.trim(),
    };

    try {
      const res = await request<Resource>("/api/v1/admin/resources", {
        method: "POST",
        body: payload,
      });

      queryClient.invalidateQueries({ queryKey: ["admin", "resources"] });
      queryClient.invalidateQueries({ queryKey: ["resources"] });

      if (onCreated) {
        onCreated(res.data);
      }
      handleClose();
    } catch (err) {
      if (err instanceof ApiError) {
        setErrorMsg(err.message || "Failed to create resource.");
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
      aria-labelledby="create-resource-dialog-title"
      maxWidth="sm"
      fullWidth
    >
      <form onSubmit={handleSubmit} noValidate>
        <DialogTitle id="create-resource-dialog-title">Create New Resource</DialogTitle>
        <DialogContent dividers>
          {errorMsg && (
            <Alert severity="error" sx={{ mb: 2 }} role="alert">
              {errorMsg}
            </Alert>
          )}

          <Box sx={{ display: "flex", flexDirection: "column", gap: 2, pt: 1 }}>
            <TextField
              id="resource-create-name"
              label="Resource Name"
              required
              fullWidth
              value={name}
              onChange={(e) => setName(e.target.value)}
              error={!!fieldErrors.name}
              helperText={fieldErrors.name || "Required, 1-100 characters"}
              inputProps={{ "aria-required": "true", maxLength: 100 }}
              disabled={isSubmitting}
            />

            <TextField
              id="resource-create-location"
              label="Location"
              required
              fullWidth
              value={location}
              onChange={(e) => setLocation(e.target.value)}
              error={!!fieldErrors.location}
              helperText={fieldErrors.location || "Required, 1-200 characters"}
              inputProps={{ "aria-required": "true", maxLength: 200 }}
              disabled={isSubmitting}
            />

            <TextField
              id="resource-create-description"
              label="Description"
              multiline
              rows={3}
              fullWidth
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              error={!!fieldErrors.description}
              helperText={fieldErrors.description || "Optional, up to 2000 characters"}
              inputProps={{ maxLength: 2000 }}
              disabled={isSubmitting}
            />
          </Box>
        </DialogContent>
        <DialogActions>
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
            {isSubmitting ? "Creating..." : "Create Resource"}
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}
