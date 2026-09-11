import {
  AppBar,
  Box,
  Toolbar,
  Typography,
  Button,
  Container,
  Link,
  Chip,
} from "@mui/material";
import { Link as RouterLink, Outlet, useNavigate } from "react-router-dom";
import { useAuth } from "../features/auth/AuthContext";

export function Layout() {
  const { user, isAuthenticated, logout } = useAuth();
  const navigate = useNavigate();

  const handleLogout = async () => {
    await logout();
    navigate("/login");
  };

  return (
    <Box sx={{ display: "flex", flexDirection: "column", minHeight: "100vh" }}>
      <AppBar position="static" color="default" elevation={1}>
        <Toolbar sx={{ justifyContent: "space-between", flexWrap: "wrap", gap: 1, py: 1 }}>
          <Box sx={{ display: "flex", alignItems: "center", gap: 2, flexWrap: "wrap" }}>
            <Typography
              variant="h6"
              component={RouterLink}
              to={isAuthenticated ? "/resources" : "/"}
              sx={{
                fontWeight: "bold",
                color: "inherit",
                textDecoration: "none",
                display: "flex",
                alignItems: "center",
              }}
            >
              CommonsBook
            </Typography>

            {isAuthenticated && (
              <Box component="nav" sx={{ display: "flex", gap: 0.5, flexWrap: "wrap" }}>
                <Button
                  component={RouterLink}
                  to="/resources"
                  color="inherit"
                  size="small"
                >
                  Resources
                </Button>
                <Button
                  component={RouterLink}
                  to="/my-bookings"
                  color="inherit"
                  size="small"
                >
                  My Bookings
                </Button>
                <Button
                  component={RouterLink}
                  to="/waitlist"
                  color="inherit"
                  size="small"
                >
                  Waitlist
                </Button>
                <Button
                  component={RouterLink}
                  to="/feedback"
                  color="inherit"
                  size="small"
                >
                  Feedback
                </Button>
                {user?.role === "admin" && (
                  <Button
                    component={RouterLink}
                    to="/admin/resources"
                    color="inherit"
                    size="small"
                    sx={{ fontWeight: "bold" }}
                  >
                    Admin
                  </Button>
                )}
              </Box>
            )}
          </Box>

          <Box sx={{ display: "flex", alignItems: "center", gap: 2 }}>
            {isAuthenticated && user ? (
              <>
                <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
                  <Typography variant="body2" fontWeight="medium">
                    {user.display_name}
                  </Typography>
                  {user.role === "admin" && (
                    <Chip label="Admin" size="small" color="secondary" />
                  )}
                </Box>
                <Button
                  variant="outlined"
                  size="small"
                  color="inherit"
                  onClick={handleLogout}
                >
                  Sign Out
                </Button>
              </>
            ) : (
              <Box sx={{ display: "flex", gap: 1 }}>
                <Button
                  component={RouterLink}
                  to="/login"
                  variant="contained"
                  color="primary"
                  size="small"
                >
                  Sign In
                </Button>
              </Box>
            )}
          </Box>
        </Toolbar>
      </AppBar>

      <Box component="main" sx={{ flexGrow: 1, py: 4 }}>
        <Container maxWidth="lg">
          <Outlet />
        </Container>
      </Box>

      <Box
        component="footer"
        sx={{
          py: 3,
          px: 2,
          mt: "auto",
          backgroundColor: "background.paper",
          borderTop: "1px solid #e0e0e0",
        }}
      >
        <Container maxWidth="lg">
          <Box
            sx={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              flexWrap: "wrap",
              gap: 2,
            }}
          >
            <Typography variant="body2" color="text.secondary">
              CommonsBook — Community Shared Resource Reservation System
            </Typography>
            <Box sx={{ display: "flex", gap: 3 }}>
              <Link
                component={RouterLink}
                to="/privacy"
                variant="body2"
                color="text.secondary"
              >
                Privacy Notice
              </Link>
            </Box>
          </Box>
        </Container>
      </Box>
    </Box>
  );
}
