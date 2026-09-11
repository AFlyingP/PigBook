import { Box } from "@mui/material";
import { UserTable } from "./UserTable";

export function AdminUsersPage() {
  return (
    <Box sx={{ py: 1 }}>
      <UserTable />
    </Box>
  );
}
