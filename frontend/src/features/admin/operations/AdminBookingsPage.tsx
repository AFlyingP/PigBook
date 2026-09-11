import { Box } from "@mui/material";
import { AdminBookingsTable } from "./AdminBookingsTable";

export function AdminBookingsPage() {
  return (
    <Box sx={{ py: 1 }}>
      <AdminBookingsTable />
    </Box>
  );
}
