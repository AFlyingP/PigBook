import { Box } from "@mui/material";
import { OutboxTable } from "./OutboxTable";

export function AdminOutboxPage() {
  return (
    <Box sx={{ py: 1 }}>
      <OutboxTable />
    </Box>
  );
}
