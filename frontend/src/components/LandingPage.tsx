import { Box, Typography, Button, Paper, Grid } from "@mui/material";
import { Link as RouterLink } from "react-router-dom";
import { useAuth } from "../features/auth/AuthContext";

export function LandingPage() {
  const { isAuthenticated } = useAuth();

  return (
    <Box sx={{ py: { xs: 3, md: 6 }, overflowX: "hidden" }}>
      <Paper
        elevation={0}
        sx={{
          p: { xs: 2, md: 8 },
          backgroundColor: "#f0f4f8",
          borderRadius: 3,
          mb: 6,
          textAlign: "center",
        }}
      >
        <Typography
          component="h1"
          variant="h3"
          fontWeight="bold"
          gutterBottom
          color="primary.dark"
        >
          CommonsBook
        </Typography>
        <Typography
          variant="h6"
          color="text.secondary"
          sx={{ maxWidth: 680, mx: "auto", mb: 2 }}
        >
          Equipment and room reservations for a single community group; accounts are invitation-only.
        </Typography>
        <Typography
          variant="body1"
          color="text.secondary"
          sx={{ maxWidth: 640, mx: "auto", mb: 4 }}
        >
          A transparent, reliable booking and availability service for community resources.
          Reserve rooms, tools, and shared facilities with strict double-booking protection.
        </Typography>

        <Box sx={{ display: "flex", gap: 2, justifyContent: "center", flexWrap: "wrap" }}>
          {isAuthenticated ? (
            <Button
              component={RouterLink}
              to="/resources"
              variant="contained"
              size="large"
              color="primary"
            >
              Browse Resources
            </Button>
          ) : (
            <>
              <Button
                component={RouterLink}
                to="/login"
                variant="contained"
                size="large"
                color="primary"
              >
                Sign In
              </Button>
              <Button
                component={RouterLink}
                to="/privacy"
                variant="outlined"
                size="large"
                color="inherit"
              >
                Privacy Notice
              </Button>
            </>
          )}
        </Box>
      </Paper>

      <Grid container spacing={4}>
        <Grid item xs={12} md={4}>
          <Paper sx={{ p: 3, height: "100%", borderRadius: 2 }}>
            <Typography variant="h6" fontWeight="bold" gutterBottom>
              Integrity by Design
            </Typography>
            <Typography variant="body2" color="text.secondary">
              Database exclusion constraints enforce that resources cannot be double-booked,
              even under high concurrency. Availability checks are honest and authoritative.
            </Typography>
          </Paper>
        </Grid>
        <Grid item xs={12} md={4}>
          <Paper sx={{ p: 3, height: "100%", borderRadius: 2 }}>
            <Typography variant="h6" fontWeight="bold" gutterBottom>
              Fair Waitlists
            </Typography>
            <Typography variant="body2" color="text.secondary">
              If a preferred time slot is reserved, join the automated waitlist. When
              cancellations occur, held offers are awarded fairly and securely.
            </Typography>
          </Paper>
        </Grid>
        <Grid item xs={12} md={4}>
          <Paper sx={{ p: 3, height: "100%", borderRadius: 2 }}>
            <Typography variant="h6" fontWeight="bold" gutterBottom>
              Invitation Pilot
            </Typography>
            <Typography variant="body2" color="text.secondary">
              Accounts are created through secure, single-use invitation tokens.
              Review our{" "}
              <RouterLink to="/privacy" style={{ color: "#1976d2" }}>
                Privacy Notice
              </RouterLink>{" "}
              for data retention and participant rights.
            </Typography>
          </Paper>
        </Grid>
      </Grid>
    </Box>
  );
}
