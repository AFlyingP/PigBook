import { useState, useMemo } from "react";
import { useParams, Link as RouterLink } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Box,
  Typography,
  Paper,
  Button,
  Chip,
  Grid,
  Skeleton,
  Alert,
  ToggleButtonGroup,
  ToggleButton,
  Divider,
  TextField,
  MenuItem,
  Card,
} from "@mui/material";
import { request } from "../../api/client";
import type { components } from "../../api/schema";
import {
  TIMEZONE,
  getNewYorkOffsetString,
  getTodayNewYorkString,
  getSevenDayWindow,
  getNewYorkInstantUtc,
  checkSlotOccupancy,
  formatInNewYork,
  validateBookingWindow,
} from "./timeUtils";

type Resource = components["schemas"]["Resource"];
type Availability = components["schemas"]["Availability"];

/**
 * Launch contract for initiating bookings (Spec 3.4, 7.1).
 * Passes resource and window without performing mutation.
 */
export interface BookingLaunchContract {
  resource: Resource;
  window: {
    starts_at: string;
    ends_at: string;
  };
}

export interface ResourceDetailProps {
  onLaunchBooking?: (contract: BookingLaunchContract) => void;
}

export function ResourceDetail({ onLaunchBooking }: ResourceDetailProps = {}) {
  const { id } = useParams<{ id: string }>();
  const [viewMode, setViewMode] = useState<"grid" | "list">("grid");
  const [selectedDayOffset, setSelectedDayOffset] = useState<number>(0);
  const [startDateStr, setStartDateStr] = useState<string>(() => getTodayNewYorkString());
  const [validationError, setValidationError] = useState<string | null>(null);

  // Timezone display information (Spec 1.2, 7.1: locked to organization)
  const tzOffset = useMemo(() => getNewYorkOffsetString(), []);

  // Fetch resource details
  const {
    data: resource,
    isLoading: isResourceLoading,
    isError: isResourceError,
    error: resourceError,
    refetch: refetchResource,
  } = useQuery<Resource>({
    queryKey: ["resources", id],
    queryFn: async () => {
      const res = await request<Resource>(`/api/v1/resources/${id}`);
      return res.data;
    },
    enabled: !!id,
  });

  // Calculate 7-day window
  const windowInfo = useMemo(() => {
    return getSevenDayWindow(startDateStr);
  }, [startDateStr]);

  // Fetch availability within the 7-day window
  const {
    data: availability,
    isLoading: isAvailLoading,
    isError: isAvailError,
    error: availError,
    refetch: refetchAvail,
  } = useQuery<Availability>({
    queryKey: ["resources", id, "availability", windowInfo.starts_at, windowInfo.ends_at],
    queryFn: async () => {
      const params = new URLSearchParams({
        starts_at: windowInfo.starts_at,
        ends_at: windowInfo.ends_at,
      });
      const res = await request<Availability>(
        `/api/v1/resources/${id}/availability?${params.toString()}`
      );
      return res.data;
    },
    enabled: !!id && !!resource?.active,
  });

  // Booking attempt handler demonstrating launch contract seam
  const handleLaunchBooking = (startIso: string, endIso: string) => {
    if (!resource || !resource.active) return;
    setValidationError(null);

    const val = validateBookingWindow(startIso, endIso);
    if (!val.valid) {
      setValidationError(val.error || "Invalid booking window");
      return;
    }

    const contract: BookingLaunchContract = {
      resource,
      window: { starts_at: startIso, ends_at: endIso },
    };

    if (onLaunchBooking) {
      onLaunchBooking(contract);
    }
  };

  if (isResourceError) {
    return (
      <Box sx={{ py: 4 }}>
        <Alert
          severity="error"
          role="alert"
          action={
            <Button color="inherit" size="small" onClick={() => refetchResource()}>
              Retry
            </Button>
          }
        >
          {resourceError instanceof Error
            ? resourceError.message
            : "Failed to load resource details. The resource may not exist or has been archived."}
        </Alert>
        <Button component={RouterLink} to="/resources" sx={{ mt: 2 }}>
          Back to Resource Catalog
        </Button>
      </Box>
    );
  }

  return (
    <Box sx={{ py: 3 }}>
      <Box sx={{ mb: 2 }}>
        <Button component={RouterLink} to="/resources" size="small" sx={{ mb: 1 }}>
          ← Back to Catalog
        </Button>
      </Box>

      {isResourceLoading || !resource ? (
        <Paper sx={{ p: 4, mb: 4 }}>
          <Skeleton variant="text" width="50%" height={40} />
          <Skeleton variant="text" width="30%" height={24} sx={{ mb: 2 }} />
          <Skeleton variant="rectangular" height={80} />
        </Paper>
      ) : (
        <Paper elevation={1} sx={{ p: 4, mb: 4, borderRadius: 2 }}>
          <Box
            sx={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "flex-start",
              flexWrap: "wrap",
              gap: 2,
              mb: 2,
            }}
          >
            <Box>
              <Typography component="h1" variant="h4" fontWeight="bold" gutterBottom>
                {resource.name}
              </Typography>
              <Typography variant="subtitle1" color="primary.main" fontWeight="medium">
                📍 {resource.location}
              </Typography>
            </Box>

            <Chip
              label={resource.active ? "Active" : "Inactive"}
              color={resource.active ? "success" : "default"}
              variant={resource.active ? "filled" : "outlined"}
            />
          </Box>

          {!resource.active && (
            <Alert severity="warning" role="alert" sx={{ mb: 3 }}>
              This resource has been archived or marked inactive. Scheduling new reservations is disabled.
            </Alert>
          )}

          <Typography variant="body1" color="text.secondary" paragraph>
            {resource.description || "No description provided for this resource."}
          </Typography>

          <Divider sx={{ my: 3 }} />

          {/* Timezone Information (Spec 1.2: America/New_York with visible offset) */}
          <Box
            sx={{
              display: "flex",
              alignItems: "center",
              gap: 2,
              flexWrap: "wrap",
              backgroundColor: "#f5f7fa",
              p: 2,
              borderRadius: 1.5,
            }}
          >
            <Typography variant="body2" fontWeight="bold">
              Organization Timezone:
            </Typography>
            <TextField
              select
              size="small"
              disabled
              value={TIMEZONE}
              helperText="Timezone display locked to organization facility"
              sx={{ minWidth: 280 }}
            >
              <MenuItem value={TIMEZONE}>
                {TIMEZONE} ({tzOffset})
              </MenuItem>
            </TextField>
          </Box>
        </Paper>
      )}

      {/* Seven-Day Availability Section */}
      <Paper elevation={1} sx={{ p: 4, borderRadius: 2 }}>
        <Box
          sx={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            flexWrap: "wrap",
            gap: 2,
            mb: 3,
          }}
        >
          <Box>
            <Typography variant="h5" fontWeight="bold">
              7-Day Availability Schedule
            </Typography>
            <Typography variant="body2" color="text.secondary">
              All times shown in {TIMEZONE} ({tzOffset}). Half-open [start, end) 30-minute intervals.
            </Typography>
          </Box>

          <Box sx={{ display: "flex", alignItems: "center", gap: 2 }}>
            <TextField
              type="date"
              label="Start Date"
              size="small"
              value={startDateStr}
              onChange={(e) => {
                if (e.target.value) setStartDateStr(e.target.value);
              }}
              InputLabelProps={{ shrink: true }}
            />

            <ToggleButtonGroup
              value={viewMode}
              exclusive
              size="small"
              onChange={(_, next) => next && setViewMode(next)}
              aria-label="Availability display mode"
            >
              <ToggleButton value="grid" aria-label="Grid view">
                Grid
              </ToggleButton>
              <ToggleButton value="list" aria-label="Accessible list view">
                Accessible List
              </ToggleButton>
            </ToggleButtonGroup>
          </Box>
        </Box>

        {/* Accessible Inline Validation Error (Spec 7.2) */}
        {validationError && (
          <Alert
            severity="error"
            role="alert"
            aria-live="polite"
            onClose={() => setValidationError(null)}
            sx={{ mb: 3 }}
          >
            {validationError}
          </Alert>
        )}

        {isAvailError && (
          <Alert
            severity="error"
            role="alert"
            sx={{ mb: 3 }}
            action={
              <Button color="inherit" size="small" onClick={() => refetchAvail()}>
                Retry
              </Button>
            }
          >
            {availError instanceof Error
              ? availError.message
              : "Failed to load availability. Please try again."}
          </Alert>
        )}

        {isAvailLoading ? (
          <Box sx={{ py: 4 }}>
            <Skeleton variant="rectangular" height={240} sx={{ borderRadius: 2 }} />
          </Box>
        ) : availability && viewMode === "grid" ? (
          /* Grid View */
          <Box>
            {/* Days Tabs */}
            <Box
              sx={{
                display: "grid",
                gridTemplateColumns: "repeat(7, 1fr)",
                gap: 1,
                mb: 2,
                overflowX: "auto",
              }}
            >
              {windowInfo.days.map((d, index) => (
                <Button
                  key={d.dateStr}
                  variant={selectedDayOffset === index ? "contained" : "outlined"}
                  size="small"
                  onClick={() => setSelectedDayOffset(index)}
                  sx={{ py: 1, textTransform: "none" }}
                >
                  <Box sx={{ textAlign: "center" }}>
                    <Typography variant="caption" display="block" fontWeight="bold">
                      {d.displayDate}
                    </Typography>
                  </Box>
                </Button>
              ))}
            </Box>

            {/* Slots for Selected Day (DST-aware instant calculation per Spec 1.2) */}
            {(() => {
              const selectedDay = windowInfo.days[selectedDayOffset];
              if (!selectedDay) return null;

              const slots = [];
              for (let h = 8; h < 20; h++) {
                for (const m of [0, 30]) {
                  const slotStartUtcStr = getNewYorkInstantUtc(selectedDay.dateStr, h, m);
                  const nextH = m === 30 ? h + 1 : h;
                  const nextM = m === 30 ? 0 : 30;
                  const slotEndUtcStr = getNewYorkInstantUtc(selectedDay.dateStr, nextH, nextM);

                  const slotStartUtc = new Date(slotStartUtcStr);
                  const slotEndUtc = new Date(slotEndUtcStr);

                  const occCheck = checkSlotOccupancy(
                    slotStartUtc,
                    slotEndUtc,
                    availability.occupied
                  );

                  slots.push({
                    timeLabel: `${h.toString().padStart(2, "0")}:${m.toString().padStart(2, "0")}`,
                    startIso: slotStartUtcStr,
                    endIso: slotEndUtcStr,
                    isOccupied: occCheck.isOccupied,
                    interval: occCheck.interval,
                  });
                }
              }

              return (
                <Box sx={{ mt: 2 }}>
                  <Typography variant="subtitle2" fontWeight="bold" sx={{ mb: 1.5 }}>
                    Available & Occupied Slots for {selectedDay.displayDate}:
                  </Typography>
                  <Grid container spacing={1.5}>
                    {slots.map((s) => (
                      <Grid item xs={6} sm={4} md={3} lg={2} key={s.startIso}>
                        <Card
                          variant="outlined"
                          sx={{
                            p: 1.5,
                            textAlign: "center",
                            backgroundColor: s.isOccupied ? "#fbe9e7" : "#e8f5e9",
                            borderColor: s.isOccupied ? "#ffab91" : "#a5d6a7",
                          }}
                        >
                          <Typography variant="body2" fontWeight="bold">
                            {s.timeLabel}
                          </Typography>
                          <Typography
                            variant="caption"
                            display="block"
                            sx={{
                              color: s.isOccupied ? "error.main" : "success.main",
                              fontWeight: "medium",
                              my: 0.5,
                            }}
                          >
                            {s.isOccupied
                              ? s.interval?.kind === "blackout"
                                ? "Blackout"
                                : "Reserved"
                              : "Available"}
                          </Typography>
                          <Button
                            size="small"
                            variant="outlined"
                            fullWidth
                            disabled={s.isOccupied || !resource?.active || !onLaunchBooking}
                            onClick={() => handleLaunchBooking(s.startIso, s.endIso)}
                            sx={{ fontSize: "0.7rem", py: 0.25 }}
                          >
                            {s.isOccupied
                              ? "Occupied"
                              : onLaunchBooking
                              ? "Select Slot"
                              : "Available"}
                          </Button>
                        </Card>
                      </Grid>
                    ))}
                  </Grid>
                </Box>
              );
            })()}
          </Box>
        ) : availability && viewMode === "list" ? (
          /* Accessible List Alternative (Spec 7.2) */
          <Box component="section" aria-label="Accessible 7-day availability schedule">
            <Typography variant="subtitle1" fontWeight="bold" sx={{ mb: 2 }}>
              Occupancy and Schedule (Accessible List View)
            </Typography>

            {availability.occupied.length === 0 ? (
              <Alert severity="info">
                All intervals within this 7-day period are currently free and available for booking.
              </Alert>
            ) : (
              <Box component="ul" sx={{ listStyle: "none", p: 0, m: 0 }}>
                {availability.occupied.map((occ, idx) => (
                  <Box
                    component="li"
                    key={idx}
                    sx={{
                      p: 2,
                      mb: 1.5,
                      borderRadius: 1.5,
                      border: "1px solid #e0e0e0",
                      backgroundColor: "#fafafa",
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      flexWrap: "wrap",
                      gap: 1,
                    }}
                  >
                    <Box>
                      <Typography variant="body2" fontWeight="bold">
                        {formatInNewYork(occ.starts_at, {
                          weekday: "short",
                          month: "short",
                          day: "numeric",
                          hour: "2-digit",
                          minute: "2-digit",
                        })}{" "}
                        –{" "}
                        {formatInNewYork(occ.ends_at, {
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </Typography>
                      <Typography variant="caption" color="text.secondary">
                        UTC: {occ.starts_at} to {occ.ends_at}
                      </Typography>
                    </Box>
                    <Chip
                      label={
                        occ.kind === "blackout"
                          ? "Administrative Blackout"
                          : `Reserved (${occ.status})`
                      }
                      color={occ.kind === "blackout" ? "warning" : "error"}
                      size="small"
                    />
                  </Box>
                ))}
              </Box>
            )}
          </Box>
        ) : null}

        {/* Booking Launch Contract Section (Spec 7.1: labeled unavailable until dialog feature registered) */}
        <Box sx={{ mt: 5, p: 3, backgroundColor: "#f0f4f8", borderRadius: 2 }}>
          <Typography variant="h6" fontWeight="bold" gutterBottom>
            Reserve this Resource
          </Typography>
          <Typography variant="body2" color="text.secondary" paragraph>
            Select an available time slot above to initialize a booking request.
          </Typography>

          <Button
            variant="contained"
            color="primary"
            disabled
            aria-label="Booking unavailable"
          >
            Book Slot (Feature registration pending)
          </Button>
          <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 1 }}>
            Reservation booking dialog will be available in an upcoming release.
          </Typography>
        </Box>
      </Paper>
    </Box>
  );
}
