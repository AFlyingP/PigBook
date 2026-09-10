import { Box, Paper, Typography, Link } from "@mui/material";
import { Link as RouterLink, useNavigate } from "react-router-dom";
import { RegisterForm } from "./RegisterForm";
import { useAuth } from "./AuthContext";
import { useEffect } from "react";

export function RegisterPage() {
  const { isAuthenticated } = useAuth();
  const navigate = useNavigate();

  useEffect(() => {
    if (isAuthenticated) {
      navigate("/resources", { replace: true });
    }
  }, [isAuthenticated, navigate]);

  return (
    <Box
      sx={{
        minHeight: "80vh",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        p: 2,
      }}
    >
      <Paper
        elevation={2}
        sx={{
          p: 4,
          width: "100%",
          maxWidth: 440,
          borderRadius: 2,
        }}
      >
        <Typography
          component="h1"
          variant="h5"
          fontWeight="bold"
          align="center"
          gutterBottom
        >
          Accept Invitation
        </Typography>
        <Typography
          variant="body2"
          color="text.secondary"
          align="center"
          sx={{ mb: 3 }}
        >
          Create your account to access shared community resources
        </Typography>

        <RegisterForm onSuccess={() => navigate("/resources", { replace: true })} />

        <Box sx={{ mt: 3, textAlign: "center" }}>
          <Link
            component={RouterLink}
            to="/privacy"
            variant="body2"
            color="text.secondary"
          >
            Privacy Notice
          </Link>
        </Box>
      </Paper>
    </Box>
  );
}
