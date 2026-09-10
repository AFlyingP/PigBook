import type { components } from "../../api/schema";

export type OccupiedInterval = components["schemas"]["OccupiedInterval"];

export const TIMEZONE = "America/New_York";

/**
 * Returns formatted offset string for America/New_York (e.g. "UTC-04:00" or "UTC-05:00").
 */
export function getNewYorkOffsetString(date: Date = new Date()): string {
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: TIMEZONE,
      timeZoneName: "shortOffset",
    }).formatToParts(date);
    const tzPart = parts.find((p) => p.type === "timeZoneName");
    if (tzPart) {
      // e.g. "GMT-4" or "GMT-5" -> "UTC-04:00"
      const match = tzPart.value.match(/GMT([+-])(\d+)(?::(\d+))?/);
      if (match) {
        const sign = match[1];
        const hours = match[2].padStart(2, "0");
        const minutes = (match[3] || "00").padStart(2, "0");
        return `UTC${sign}${hours}:${minutes}`;
      }
      return tzPart.value.replace("GMT", "UTC");
    }
  } catch {
    // fallback
  }
  return "UTC-04:00";
}

/**
 * Formats a timestamp in America/New_York timezone.
 */
export function formatInNewYork(
  dateOrIso: Date | string,
  options?: Intl.DateTimeFormatOptions
): string {
  const d = typeof dateOrIso === "string" ? new Date(dateOrIso) : dateOrIso;
  return new Intl.DateTimeFormat("en-US", {
    timeZone: TIMEZONE,
    ...options,
  }).format(d);
}

/**
 * Returns the current date in America/New_York as YYYY-MM-DD.
 */
export function getTodayNewYorkString(now: Date = new Date()): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: TIMEZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);

  const year = parts.find((p) => p.type === "year")?.value;
  const month = parts.find((p) => p.type === "month")?.value;
  const day = parts.find((p) => p.type === "day")?.value;
  return `${year}-${month}-${day}`;
}

/**
 * Compute the UTC instant for midnight (00:00:00) in America/New_York for a given YYYY-MM-DD date.
 * Accurately handles DST transitions by verifying local hour/minute in America/New_York.
 */
export function getNewYorkMidnightUtc(dateStr: string): string {
  const [year, month, day] = dateStr.split("-").map(Number);
  // America/New_York midnight is either 04:00 UTC (EDT) or 05:00 UTC (EST)
  for (const hour of [4, 5, 3, 6]) {
    const d = new Date(Date.UTC(year, month - 1, day, hour, 0, 0, 0));
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: TIMEZONE,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).formatToParts(d);

    const pYear = Number(parts.find((p) => p.type === "year")?.value);
    const pMonth = Number(parts.find((p) => p.type === "month")?.value);
    const pDay = Number(parts.find((p) => p.type === "day")?.value);
    const pHour = Number(parts.find((p) => p.type === "hour")?.value) % 24;
    const pMin = Number(parts.find((p) => p.type === "minute")?.value);

    if (pYear === year && pMonth === month && pDay === day && pHour === 0 && pMin === 0) {
      return d.toISOString();
    }
  }

  // Fallback EDT
  return new Date(Date.UTC(year, month - 1, day, 4, 0, 0, 0)).toISOString();
}

export interface DayInfo {
  dateStr: string; // YYYY-MM-DD
  displayDate: string; // e.g. "Mon, Sep 14"
  starts_at: string; // UTC ISO string
  ends_at: string; // UTC ISO string
}

/**
 * Generates a 7-day schedule window starting on startDateStr (YYYY-MM-DD in New York).
 * Guarantees that total span does not exceed 7 days (168 hours) to satisfy backend validation.
 */
export function getSevenDayWindow(startDateStr: string): {
  starts_at: string;
  ends_at: string;
  days: DayInfo[];
} {
  const [year, month, day] = startDateStr.split("-").map(Number);
  const days: DayInfo[] = [];

  for (let i = 0; i < 7; i++) {
    // Increment date
    const d = new Date(Date.UTC(year, month - 1, day + i, 12, 0, 0));
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: TIMEZONE,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      weekday: "short",
      monthName: "short",
    } as Intl.DateTimeFormatOptions).formatToParts(d);

    const y = parts.find((p) => p.type === "year")?.value;
    const m = parts.find((p) => p.type === "month")?.value;
    const dt = parts.find((p) => p.type === "day")?.value;
    const weekday = parts.find((p) => p.type === "weekday")?.value || "";
    const dateKey = `${y}-${m}-${dt}`;

    const dayStartUtc = getNewYorkMidnightUtc(dateKey);
    // Next day midnight
    const nextD = new Date(Date.UTC(year, month - 1, day + i + 1, 12, 0, 0));
    const nextParts = new Intl.DateTimeFormat("en-US", {
      timeZone: TIMEZONE,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(nextD);
    const ny = nextParts.find((p) => p.type === "year")?.value;
    const nm = nextParts.find((p) => p.type === "month")?.value;
    const ndt = nextParts.find((p) => p.type === "day")?.value;
    const nextDateKey = `${ny}-${nm}-${ndt}`;
    const dayEndUtc = getNewYorkMidnightUtc(nextDateKey);

    days.push({
      dateStr: dateKey,
      displayDate: `${weekday}, ${m}/${dt}`,
      starts_at: dayStartUtc,
      ends_at: dayEndUtc,
    });
  }

  const windowStart = days[0].starts_at;
  let windowEnd = days[6].ends_at;

  // Enforce max span <= 7 days (168h) across DST
  const startMs = new Date(windowStart).getTime();
  const endMs = new Date(windowEnd).getTime();
  const maxSpanMs = 7 * 24 * 60 * 60 * 1000;
  if (endMs - startMs > maxSpanMs) {
    windowEnd = new Date(startMs + maxSpanMs).toISOString();
  }

  return {
    starts_at: windowStart,
    ends_at: windowEnd,
    days,
  };
}

/**
 * Checks if a time slot [slotStart, slotEnd) overlaps any occupied intervals.
 */
export function checkSlotOccupancy(
  slotStart: Date,
  slotEnd: Date,
  occupied: OccupiedInterval[] = []
): { isOccupied: boolean; interval?: OccupiedInterval } {
  for (const item of occupied) {
    const occStart = new Date(item.starts_at);
    const occEnd = new Date(item.ends_at);
    // Half-open interval overlap: [slotStart, slotEnd) overlaps [occStart, occEnd)
    // iff slotStart < occEnd && slotEnd > occStart
    if (slotStart < occEnd && slotEnd > occStart) {
      return { isOccupied: true, interval: item };
    }
  }
  return { isOccupied: false };
}

/**
 * Validates 30-minute alignment, duration (30 min - 4 hours), lead time, and horizon (Spec 1.2).
 */
export function validateBookingWindow(
  startIso: string,
  endIso: string,
  now: Date = new Date()
): { valid: boolean; error?: string } {
  const start = new Date(startIso);
  const end = new Date(endIso);

  if (isNaN(start.getTime()) || isNaN(end.getTime())) {
    return { valid: false, error: "Invalid timestamp format" };
  }

  // Check 30-minute alignment (minutes 0 or 30, seconds 0, ms 0)
  if (
    start.getUTCMinutes() % 30 !== 0 ||
    start.getUTCSeconds() !== 0 ||
    start.getUTCMilliseconds() !== 0
  ) {
    return {
      valid: false,
      error: "Start time must fall on a UTC 30-minute boundary with zero seconds",
    };
  }

  if (
    end.getUTCMinutes() % 30 !== 0 ||
    end.getUTCSeconds() !== 0 ||
    end.getUTCMilliseconds() !== 0
  ) {
    return {
      valid: false,
      error: "End time must fall on a UTC 30-minute boundary with zero seconds",
    };
  }

  if (end <= start) {
    return { valid: false, error: "End time must be strictly after start time" };
  }

  const durationMinutes = (end.getTime() - start.getTime()) / (1000 * 60);
  if (durationMinutes < 30 || durationMinutes > 240) {
    return {
      valid: false,
      error: "Booking duration must be between 30 minutes and 4 hours inclusive",
    };
  }

  // Lead time: at least 15 minutes in the future
  const minFuture = new Date(now.getTime() + 15 * 60 * 1000);
  if (start < minFuture) {
    return {
      valid: false,
      error: "Start time must be at least 15 minutes in the future",
    };
  }

  // Horizon: at most 90 days ahead
  const maxFuture = new Date(now.getTime() + 90 * 24 * 60 * 60 * 1000);
  if (start > maxFuture) {
    return {
      valid: false,
      error: "Start time cannot be more than 90 days in the future",
    };
  }

  return { valid: true };
}
