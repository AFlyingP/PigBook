import { useEffect, useState } from "react";
import { Box, CircularProgress, Typography, Alert } from "@mui/material";
import { Navigate, useLocation, Outlet } from "react-router-dom";
import { useAuth } from "../features/auth/AuthContext";

interface RequireAuthProps {
  adminOnly?: boolean;
}

export function RequireAuth({ adminOnly = false }: RequireAuthProps) {
  const { user, isAuthenticated, isLoading, checkSession } = useAuth();
  const location = useLocation();
  const [checking, setChecking] = useState(!isAuthenticated);

  useEffect(() => {
    let active = true;
    if (!isAuthenticated) {
      checkSession().finally(() => {
        if (active) {
          setChecking(false);
        }
      });
    } else {
      setChecking(false);
    }
    return () => {
      active = false;
    };
  }, [isAuthenticated, checkSession]);

  if (isLoading || checking) {
    return (
      <Box
        sx={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          minHeight: "50vh",
          gap: 2,
        }}
      >
        <CircularProgress aria-label="Loading session..." />
        <Typography variant="body2" color="text.secondary">
          Checking authentication...
        </Typography>
      </Box>
    );
  }

  if (!isAuthenticated || !user) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  if (adminOnly && user.role !== "admin") {
    return (
      <Box sx={{ py: 4 }}>
        <Alert severity="error" role="alert">
          <Typography variant="h6" gutterBottom>
            403 Forbidden
          </Typography>
          <Typography variant="body2">
            Administrator privileges are required to access this section.
          </Typography>
        </Alert>
      </Box>
    );
  }

  return <Outlet />;
}
