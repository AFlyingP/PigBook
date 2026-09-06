import CssBaseline from "@mui/material/CssBaseline";
import Container from "@mui/material/Container";
import Typography from "@mui/material/Typography";
import Box from "@mui/material/Box";

export default function App() {
  return (
    <>
      <CssBaseline />
      <Container maxWidth="md">
        <Box sx={{ my: 4 }}>
          <Typography variant="h3" component="h1" gutterBottom>
            CommonsBook
          </Typography>
          <Typography variant="body1">
            Equipment and room reservations for a single community group; accounts are invitation-only.
          </Typography>
        </Box>
      </Container>
    </>
  );
}
