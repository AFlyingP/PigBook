import React, { useState } from "react";
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

interface LoginFormProps {
  onSuccess?: () => void;
}

export function LoginForm({ onSuccess }: LoginFormProps) {
  const { login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<{ email?: string; password?: string }>({});
  const [isSubmitting, setIsSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setFieldErrors({});

    const newFieldErrors: { email?: string; password?: string } = {};
    if (!email.trim()) {
      newFieldErrors.email = "Email is required";
    }
    if (!password) {
      newFieldErrors.password = "Password is required";
    }

    if (Object.keys(newFieldErrors).length > 0) {
      setFieldErrors(newFieldErrors);
      return;
    }

    setIsSubmitting(true);
    try {
      await login(email.trim(), password);
      if (onSuccess) {
        onSuccess();
      }
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 401) {
          setError("Invalid email or password, or account disabled.");
        } else if (err.status === 429) {
          setError("Too many attempts. Please wait a moment before trying again.");
        } else {
          setError(err.message || "Login failed. Please try again.");
        }
      } else {
        setError("An unexpected network error occurred. Please try again.");
      }
    } finally {
      setIsSubmitting(false);
    }
  };

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
        id="login-email"
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
          "aria-errormessage": fieldErrors.email ? "login-email-error" : undefined,
        }}
        FormHelperTextProps={{
          id: "login-email-error",
        }}
      />

      <TextField
        id="login-password"
        label="Password"
        name="password"
        type={showPassword ? "text" : "password"}
        autoComplete="current-password"
        required
        fullWidth
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        error={Boolean(fieldErrors.password)}
        helperText={fieldErrors.password}
        inputProps={{
          "aria-invalid": Boolean(fieldErrors.password),
          "aria-errormessage": fieldErrors.password ? "login-password-error" : undefined,
        }}
        FormHelperTextProps={{
          id: "login-password-error",
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
          <CircularProgress size={24} color="inherit" aria-label="Logging in..." />
        ) : (
          "Sign In"
        )}
      </Button>
    </Box>
  );
}
