export interface CreateAttempt {
  key: string;
  payload: unknown;
  kind: "booking" | "blackout";
  principalId: string;
  createdAt: string;
}

const STORAGE_KEY = "commonsbook_create_attempt";

function generateUuid(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

export function beginAttempt(
  principalId: string,
  kind: "booking" | "blackout",
  payload: unknown
): CreateAttempt {
  const attempt: CreateAttempt = {
    key: generateUuid(),
    payload,
    kind,
    principalId,
    createdAt: new Date().toISOString(),
  };

  try {
    if (typeof sessionStorage !== "undefined") {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(attempt));
    }
  } catch {
    // Storage access might be restricted
  }

  return attempt;
}

export function restoreAttempt(principalId: string): CreateAttempt | null {
  try {
    if (typeof sessionStorage === "undefined") {
      return null;
    }
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return null;
    }
    const attempt = JSON.parse(raw) as CreateAttempt;
    if (!attempt || typeof attempt !== "object") {
      clearAttempt();
      return null;
    }

    // Attempt belongs only to the matching authenticated principal
    if (attempt.principalId !== principalId) {
      clearAttempt();
      return null;
    }

    // Return attempt with createdAt intact regardless of age.
    // Dialog enforces Spec 7.3 24-hour rule: under 24h same-key retry;
    // at/over 24h automatic replay stops and user is prompted to check bookings before new attempt.
    return attempt;
  } catch {
    clearAttempt();
    return null;
  }
}

export function clearAttempt(): void {
  try {
    if (typeof sessionStorage !== "undefined") {
      sessionStorage.removeItem(STORAGE_KEY);
    }
  } catch {
    // ignore
  }
}
