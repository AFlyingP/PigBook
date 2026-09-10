import { Box, Typography, Paper, Divider } from "@mui/material";

export function PrivacyPage() {
  return (
    <Box sx={{ py: 4 }}>
      <Paper elevation={1} sx={{ p: { xs: 3, md: 5 }, borderRadius: 2 }}>
        <Typography component="h1" variant="h4" fontWeight="bold" gutterBottom>
          Privacy Notice
        </Typography>
        <Typography variant="caption" color="text.secondary" display="block" gutterBottom>
          Version 2026-09-v1 • Effective September 2026
        </Typography>

        <Divider sx={{ my: 3 }} />

        <Typography variant="h6" fontWeight="bold" gutterBottom>
          1. Purpose and Scope
        </Typography>
        <Typography variant="body1" paragraph>
          CommonsBook is a community resource reservation pilot. This notice explains what
          personal data we collect, how it is used to coordinate bookings, and how your privacy
          is protected.
        </Typography>

        <Typography variant="h6" fontWeight="bold" gutterBottom>
          2. Information We Collect
        </Typography>
        <Typography variant="body1" paragraph>
          • <strong>Account Information:</strong> Email address, display name, and hashed passwords.
          Passwords are never stored in plaintext and are hashed using modern Argon2id algorithms.
          <br />
          • <strong>Reservation Data:</strong> Scheduled resource slots, booking statuses, and
          waitlist entries. Booking owners are visible to administrators for facility management
          but are not displayed to other community members.
          <br />
          • <strong>Operational Telemetry:</strong> Anonymized server logs, sanitized error reports,
          and rate-limit audit records. Access tokens exist in browser memory only and are never
          persisted to browser storage.
        </Typography>

        <Typography variant="h6" fontWeight="bold" gutterBottom>
          3. Retention and Deletion
        </Typography>
        <Typography variant="body1" paragraph>
          Inactive accounts and bookings are subject to scheduled retention policies. Upon
          participant withdrawal, account records are pseudonymized and authentication credentials
          are revoked.
        </Typography>

        <Typography variant="h6" fontWeight="bold" gutterBottom>
          4. Contact and Operator Inquiries
        </Typography>
        <Typography variant="body1">
          For questions regarding your account or to request account deletion, contact the
          designated pilot operator through your community administrator.
        </Typography>
      </Paper>
    </Box>
  );
}
