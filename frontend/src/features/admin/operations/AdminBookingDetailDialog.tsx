
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  Box,
  Typography,
  Chip,
  Divider,
} from "@mui/material";
import type { components } from "../../../api/schema";

type Booking = components["schemas"]["Booking"];

interface AdminBookingDetailDialogProps {
  open: boolean;
  booking: Booking | null;
  onClose: () => void;
  onCancelRequested?: (booking: Booking) => void;
}

export function AdminBookingDetailDialog({
  open,
  booking,
  onClose,
  onCancelRequested,
}: AdminBookingDetailDialogProps) {
  if (!booking) return null;

  const isCancellable =
    booking.status === "confirmed" || booking.status === "offered";

  return (
    <Dialog
      open={open}
      onClose={onClose}
      aria-labelledby="booking-detail-dialog-title"
      maxWidth="sm"
      fullWidth
    >
      <DialogTitle id="booking-detail-dialog-title">
        Reservation Details
      </DialogTitle>
      <DialogContent dividers>
        <Box sx={{ display: "flex", flexDirection: "column", gap: 1.5 }}>
          <Box sx={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <Typography variant="body2" color="text.secondary">
              Status
            </Typography>
            <Chip
              label={booking.status.toUpperCase()}
              size="small"
              color={
                booking.status === "confirmed"
                  ? "success"
                  : booking.status === "offered"
                  ? "primary"
                  : "default"
              }
              variant="outlined"
            />
          </Box>

          <Divider />

          <Box>
            <Typography variant="caption" color="text.secondary">
              Booking ID
            </Typography>
            <Typography variant="body2" fontFamily="monospace">
              {booking.id}
            </Typography>
          </Box>

          <Box>
            <Typography variant="caption" color="text.secondary">
              Resource ID
            </Typography>
            <Typography variant="body2" fontFamily="monospace">
              {booking.resource_id}
            </Typography>
          </Box>

          <Box>
            <Typography variant="caption" color="text.secondary">
              User ID
            </Typography>
            <Typography variant="body2" fontFamily="monospace">
              {booking.user_id || "N/A"}
            </Typography>
          </Box>

          <Box>
            <Typography variant="caption" color="text.secondary">
              Kind
            </Typography>
            <Typography variant="body2">{booking.kind}</Typography>
          </Box>

          <Box>
            <Typography variant="caption" color="text.secondary">
              Start Time (UTC)
            </Typography>
            <Typography variant="body2">{new Date(booking.starts_at).toISOString()}</Typography>
          </Box>

          <Box>
            <Typography variant="caption" color="text.secondary">
              End Time (UTC)
            </Typography>
            <Typography variant="body2">{new Date(booking.ends_at).toISOString()}</Typography>
          </Box>

          {booking.expires_at && (
            <Box>
              <Typography variant="caption" color="text.secondary">
                Offer Expiration (UTC)
              </Typography>
              <Typography variant="body2">{new Date(booking.expires_at).toISOString()}</Typography>
            </Box>
          )}

          {booking.cancellation_reason && (
            <Box>
              <Typography variant="caption" color="text.secondary">
                Cancellation Reason
              </Typography>
              <Typography variant="body2">{booking.cancellation_reason}</Typography>
            </Box>
          )}

          <Box sx={{ display: "flex", justifyContent: "space-between" }}>
            <Box>
              <Typography variant="caption" color="text.secondary">
                Version
              </Typography>
              <Typography variant="body2">v{booking.version}</Typography>
            </Box>
            <Box>
              <Typography variant="caption" color="text.secondary">
                Created At
              </Typography>
              <Typography variant="body2">{new Date(booking.created_at).toLocaleString()}</Typography>
            </Box>
          </Box>
        </Box>
      </DialogContent>
      <DialogActions>
        {isCancellable && onCancelRequested && (
          <Button
            onClick={() => {
              onClose();
              onCancelRequested(booking);
            }}
            color="error"
          >
            Cancel Reservation
          </Button>
        )}
        <Button onClick={onClose} color="inherit">
          Close
        </Button>
      </DialogActions>
    </Dialog>
  );
}
