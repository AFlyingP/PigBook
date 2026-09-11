import React, { useState } from "react";
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  TextField,
  FormControl,
  InputLabel,
  Select,
  MenuItem,
  Alert,
  Box,
  CircularProgress,
  Typography,
} from "@mui/material";
import { request, ApiError } from "../../../api/client";
import type { components } from "../../../api/schema";

type InviteCreate = components["schemas"]["InviteCreate"];
type InvitationResult = components["schemas"]["InvitationResult"];

interface InvitationDialogProps {
  open: boolean;
  onClose: () => void;
  onInvited?: () => void;
}

export function InvitationDialog({ open, onClose, onInvited }: InvitationDialogProps) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<"member" | "admin">("member");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<{ [k: string]: string }>({});

  // Single-use component state for the invitation url - cleared on close, never stored in persistent storage
  const [invitationResult, setInvitationResult] = useState<InvitationResult | null>(null);
  const [copied, setCopied] = useState(false);

  const handleClose = () => {
    if (isSubmitting) return;
    setEmail("");
    setRole("member");
    setErrorMsg(null);
    setFieldErrors({});
    // Security requirement: clear link from component state when dialog closes
    setInvitationResult(null);
    setCopied(false);
    onClose();
  };

  const validate = () => {
    const errs: { [k: string]: string } = {};
    if (!email.trim()) {
      errs.email = "Email is required";
    } else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim())) {
      errs.email = "Please enter a valid email address";
    }
    setFieldErrors(errs);
    return Object.keys(errs).length === 0;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!validate()) return;

    setIsSubmitting(true);
    setErrorMsg(null);

    const payload: InviteCreate = {
      email: email.trim(),
      role,
    };

    try {
      const res = await request<InvitationResult>("/api/v1/admin/invitations", {
        method: "POST",
        body: payload,
      });

      setInvitationResult(res.data);
      if (onInvited) {
        onInvited();
      }
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 409) {
          setErrorMsg("A user with this email address already exists.");
        } else {
          setErrorMsg(err.message || "Failed to create invitation.");
        }
      } else {
        setErrorMsg("A network or unexpected error occurred.");
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleCopyLink = async () => {
    if (!invitationResult?.invitation_url) return;
    try {
      await navigator.clipboard.writeText(invitationResult.invitation_url);
      setCopied(true);
      setTimeout(() => setCopied(false), 3000);
    } catch {
      // clipboard write might fail in some contexts
    }
  };

  return (
    <Dialog
      open={open}
      onClose={handleClose}
      aria-labelledby="invitation-dialog-title"
      maxWidth="sm"
      fullWidth
    >
      <DialogTitle id="invitation-dialog-title">
        {invitationResult ? "Invitation Link Generated" : "Invite New User"}
      </DialogTitle>

      {invitationResult ? (
        <>
          <DialogContent dividers>
            <Alert severity="success" sx={{ mb: 2 }}>
              Invitation generated for <strong>{invitationResult.email}</strong> as{" "}
              <strong>{invitationResult.role}</strong>.
            </Alert>

            <Typography variant="body2" sx={{ mb: 1 }}>
              Copy the invitation link below. For security, this link is shown only once and will
              be cleared when this dialog is closed:
            </Typography>

            <TextField
              id="invitation-url-display"
              fullWidth
              value={invitationResult.invitation_url}
              InputProps={{
                readOnly: true,
              }}
              sx={{ my: 1, fontFamily: "monospace" }}
            />

            <Typography variant="caption" color="text.secondary">
              Expires: {new Date(invitationResult.expires_at).toLocaleString()}
            </Typography>
          </DialogContent>
          <DialogActions>
            <Button
              onClick={handleCopyLink}
              variant="contained"
              color={copied ? "success" : "primary"}
              id="copy-invite-link-btn"
            >
              {copied ? "Link Copied!" : "Copy Link"}
            </Button>
            <Button onClick={handleClose} color="inherit">
              Done
            </Button>
          </DialogActions>
        </>
      ) : (
        <form onSubmit={handleSubmit} noValidate>
          <DialogContent dividers>
            {errorMsg && (
              <Alert severity="error" sx={{ mb: 2 }} role="alert">
                {errorMsg}
              </Alert>
            )}

            <Box sx={{ display: "flex", flexDirection: "column", gap: 2, pt: 1 }}>
              <TextField
                id="invite-email"
                label="Email Address"
                type="email"
                required
                fullWidth
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                error={!!fieldErrors.email}
                helperText={fieldErrors.email || "Recipient email"}
                inputProps={{ "aria-required": "true" }}
                disabled={isSubmitting}
              />

              <FormControl size="small" fullWidth>
                <InputLabel id="invite-role-label">Assigned Role</InputLabel>
                <Select
                  labelId="invite-role-label"
                  id="invite-role"
                  value={role}
                  label="Assigned Role"
                  onChange={(e) => setRole(e.target.value as "member" | "admin")}
                  disabled={isSubmitting}
                >
                  <MenuItem value="member">Member</MenuItem>
                  <MenuItem value="admin">Administrator</MenuItem>
                </Select>
              </FormControl>
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
              {isSubmitting ? "Generating..." : "Generate Invitation"}
            </Button>
          </DialogActions>
        </form>
      )}
    </Dialog>
  );
}
