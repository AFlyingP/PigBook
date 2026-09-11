import React, { createContext, useContext, useEffect, useState, useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  type User,
  request,
  setAccessToken,
  refreshSession,
  subscribeToAuthChanges,
  postAuthBroadcast,
} from "../../api/client";
import { clearAttempt } from "../../api/createAttempt";

export interface AuthContextType {
  user: User | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (
    invitationToken: string,
    email: string,
    password: string,
    displayName: string
  ) => Promise<void>;
  logout: () => Promise<void>;
  checkSession: () => Promise<User | null>;
  refetchMe: () => Promise<void>;
}

// eslint-disable-next-line react-refresh/only-export-components
export const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [hasCheckedSession, setHasCheckedSession] = useState<boolean>(false);
  const queryClient = useQueryClient();

  const handleLogoutCleanup = useCallback(() => {
    setAccessToken(null);
    setUser(null);
    clearAttempt();
    queryClient.clear();
  }, [queryClient]);

  // Listen to cross-tab auth state changes
  useEffect(() => {
    const unsubscribe = subscribeToAuthChanges((token, newUser) => {
      if (!token) {
        handleLogoutCleanup();
      } else if (newUser) {
        setUser(newUser);
      }
    });
    return unsubscribe;
  }, [handleLogoutCleanup]);

  const checkSession = useCallback(async (): Promise<User | null> => {
    if (user) return user;
    if (hasCheckedSession) return null;

    setIsLoading(true);
    try {
      await refreshSession();
      const meRes = await request<User>("/api/v1/me");
      setUser(meRes.data);
      return meRes.data;
    } catch {
      handleLogoutCleanup();
      return null;
    } finally {
      setIsLoading(false);
      setHasCheckedSession(true);
    }
  }, [user, hasCheckedSession, handleLogoutCleanup]);

  const refetchMe = useCallback(async () => {
    try {
      const res = await request<User>("/api/v1/me");
      setUser(res.data);
    } catch {
      handleLogoutCleanup();
    }
  }, [handleLogoutCleanup]);

  const login = useCallback(
    async (email: string, password: string): Promise<void> => {
      setIsLoading(true);
      try {
        const res = await request<{
          access_token: string;
          token_type: string;
          expires_in: number;
          user: User;
        }>("/api/v1/auth/login", {
          method: "POST",
          body: { email, password },
          skipAuth: true,
        });

        setAccessToken(res.data.access_token);
        setUser(res.data.user);
        setHasCheckedSession(true);
        postAuthBroadcast({
          type: "TOKEN_REFRESHED",
          token: res.data.access_token,
          user: res.data.user,
        });
      } finally {
        setIsLoading(false);
      }
    },
    []
  );

  const register = useCallback(
    async (
      invitationToken: string,
      email: string,
      password: string,
      displayName: string
    ): Promise<void> => {
      setIsLoading(true);
      try {
        await request<User>("/api/v1/auth/register", {
          method: "POST",
          body: {
            invitation_token: invitationToken,
            email,
            password,
            display_name: displayName,
          },
          skipAuth: true,
        });

        // Auto-login after successful registration
        await login(email, password);
      } finally {
        setIsLoading(false);
      }
    },
    [login]
  );

  const logout = useCallback(async (): Promise<void> => {
    try {
      await request("/api/v1/auth/logout", {
        method: "POST",
      });
    } catch {
      // Best-effort server-side logout
    } finally {
      handleLogoutCleanup();
      postAuthBroadcast({ type: "LOGOUT" });
    }
  }, [handleLogoutCleanup]);

  const value: AuthContextType = {
    user,
    isLoading,
    isAuthenticated: !!user,
    login,
    register,
    logout,
    checkSession,
    refetchMe,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthContextType {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return ctx;
}
