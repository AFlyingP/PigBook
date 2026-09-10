import React, { useState, useEffect } from "react";
import {
  Box,
  Button,
  TextField,
  Typography,
  Alert,
  IconButton,
  InputAdornment,
  CircularProgress,
} from "@mui/material";
import { useAuth } from "./AuthContext";
import { ApiError } from "../../api/client";

interface RegisterFormProps {
  onSuccess?: () => void;
}

export function RegisterForm({ onSuccess }: RegisterFormProps) {
  const { register } = useAuth();
  const [token, setToken] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<{
    email?: string;
    displayName?: string;
    password?: string;
  }>({});
  const [isSubmitting, setIsSubmitting] = useState(false);

  // Read invitation token once from URL fragment and immediately erase it via replaceState
  useEffect(() => {
    const hash = window.location.hash;
    if (hash && hash.includes("token=")) {
      const params = new URLSearchParams(hash.replace(/^#/, ""));
      const extractedToken = params.get("token");
      if (extractedToken) {
        setToken(extractedToken);
      }
      // Erase fragment from the URL immediately (Spec 7.1)
      window.history.replaceState(
        null,
        "",
        window.location.pathname + window.location.search
      );
    }
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setFieldErrors({});

    if (!token) {
      setError("An invitation token is required. Please use the complete link from your invitation email.");
      return;
    }

    const newFieldErrors: {
      email?: string;
      displayName?: string;
      password?: string;
    } = {};

    if (!email.trim()) {
      newFieldErrors.email = "Email is required";
    }
    if (!displayName.trim()) {
      newFieldErrors.displayName = "Display name is required";
    } else if (displayName.trim().length > 80) {
      newFieldErrors.displayName = "Display name must be 80 characters or fewer";
    }

    // Enforce 12..128 Unicode codepoints per Spec 8.1
    const passwordCodepoints = [...password].length;
    if (!password) {
      newFieldErrors.password = "Password is required";
    } else if (passwordCodepoints < 12) {
      newFieldErrors.password = "Password must be at least 12 characters long";
    } else if (passwordCodepoints > 128) {
      newFieldErrors.password = "Password must be 128 characters or fewer";
    }

    if (Object.keys(newFieldErrors).length > 0) {
      setFieldErrors(newFieldErrors);
      return;
    }

    setIsSubmitting(true);
    try {
      await register(token, email.trim(), password, displayName.trim());
      if (onSuccess) {
        onSuccess();
      }
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 422) {
          if (err.code === "INVALID_INVITATION") {
            setError("Invalid or expired invitation token.");
          } else if (err.code === "VALIDATION_ERROR") {
            let detailMsg = "Validation error: please verify your email and display name.";
            if (err.details && typeof err.details === "object") {
              const entries = Object.entries(err.details);
              if (entries.length > 0) {
                detailMsg = `Validation error: ${entries.map(([k, v]) => `${k}: ${v}`).join(", ")}`;
              }
            }
            setError(detailMsg);
          } else {
            setError(err.message || "Invalid registration request.");
          }
        } else if (err.status === 409) {
          setError("An account with this email address already exists.");
        } else {
          setError(err.message || "Registration failed. Please try again.");
        }
      } else {
        setError("An unexpected network error occurred. Please try again.");
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  if (token === null) {
    return (
      <Box sx={{ maxWidth: 450, mx: "auto", mt: 4 }}>
        <Alert severity="warning" role="alert">
          <Typography variant="subtitle1" fontWeight="bold" gutterBottom>
            Invitation Required
          </Typography>
          <Typography variant="body2">
            CommonsBook registration is by invitation only. Please use the full invitation link sent to your email to complete registration.
          </Typography>
        </Alert>
      </Box>
    );
  }

  return (
    <Box
      component="form"
      onSubmit={handleSubmit}
      noValidate
      sx={{
        width: "100%",
        maxWidth: 400,
        mx: "auto",
        display: "flex",
        flexDirection: "column",
        gap: 2.5,
      }}
    >
      {error && (
        <Alert severity="error" role="alert" aria-live="polite">
          {error}
        </Alert>
      )}

      <TextField
        id="register-email"
        label="Email address"
        name="email"
        type="email"
        autoComplete="email"
        autoFocus
        required
        fullWidth
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        error={Boolean(fieldErrors.email)}
        helperText={fieldErrors.email}
        inputProps={{
          "aria-invalid": Boolean(fieldErrors.email),
          "aria-errormessage": fieldErrors.email ? "register-email-error" : undefined,
        }}
        FormHelperTextProps={{
          id: "register-email-error",
        }}
      />

      <TextField
        id="register-display-name"
        label="Display name"
        name="displayName"
        type="text"
        autoComplete="name"
        required
        fullWidth
        value={displayName}
        onChange={(e) => setDisplayName(e.target.value)}
        error={Boolean(fieldErrors.displayName)}
        helperText={fieldErrors.displayName}
        inputProps={{
          maxLength: 80,
          "aria-invalid": Boolean(fieldErrors.displayName),
          "aria-errormessage": fieldErrors.displayName ? "register-name-error" : undefined,
        }}
        FormHelperTextProps={{
          id: "register-name-error",
        }}
      />

      <TextField
        id="register-password"
        label="Password (min 12 characters)"
        name="password"
        type={showPassword ? "text" : "password"}
        autoComplete="new-password"
        required
        fullWidth
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        error={Boolean(fieldErrors.password)}
        helperText={fieldErrors.password || "Must be at least 12 Unicode codepoints"}
        inputProps={{
          minLength: 12,
          maxLength: 128,
          "aria-invalid": Boolean(fieldErrors.password),
          "aria-errormessage": fieldErrors.password ? "register-password-error" : undefined,
        }}
        FormHelperTextProps={{
          id: "register-password-error",
        }}
        InputProps={{
          endAdornment: (
            <InputAdornment position="end">
              <IconButton
                aria-label={showPassword ? "Hide password" : "Show password"}
                onClick={() => setShowPassword(!showPassword)}
                edge="end"
                size="small"
              >
                <Typography variant="caption" sx={{ userSelect: "none" }}>
                  {showPassword ? "Hide" : "Show"}
                </Typography>
              </IconButton>
            </InputAdornment>
          ),
        }}
      />

      <Button
        type="submit"
        variant="contained"
        color="primary"
        size="large"
        fullWidth
        disabled={isSubmitting}
        sx={{ mt: 1, py: 1.5 }}
      >
        {isSubmitting ? (
          <CircularProgress size={24} color="inherit" aria-label="Creating account..." />
        ) : (
          "Complete Registration"
        )}
      </Button>
    </Box>
  );
}
