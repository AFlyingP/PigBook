import { createBrowserRouter, Navigate, RouteObject } from "react-router-dom";
import { Layout } from "./components/Layout";
import { RequireAuth } from "./components/RequireAuth";
import { LandingPage } from "./components/LandingPage";
import { PrivacyPage } from "./components/PrivacyPage";
import { NotFoundPage } from "./components/NotFoundPage";
import { LoginPage } from "./features/auth/LoginPage";
import { RegisterPage } from "./features/auth/RegisterPage";
import { ResourceList } from "./features/resources/ResourceList";
import { ResourceDetail } from "./features/resources/ResourceDetail";
import { MyBookingsPage } from "./features/bookings/MyBookingsPage";
import { WaitlistPage } from "./features/waitlist/WaitlistPage";
import { FeedbackPage } from "./features/feedback/FeedbackPage";
import { AdminLayout } from "./features/admin/AdminLayout";
import { AdminResourcesPage } from "./features/admin/inventory/AdminResourcesPage";
import { AdminBlackoutsPage } from "./features/admin/inventory/AdminBlackoutsPage";
import { AdminBookingsPage } from "./features/admin/operations/AdminBookingsPage";
import { AdminUsersPage } from "./features/admin/operations/AdminUsersPage";
import { AdminAuditPage } from "./features/admin/operations/AdminAuditPage";
import { AdminOutboxPage } from "./features/admin/operations/AdminOutboxPage";
import { AdminFeedbackPage } from "./features/feedback/AdminFeedbackPage";

export const routes: RouteObject[] = [
  {
    path: "/",
    element: <Layout />,
    children: [
      {
        index: true,
        element: <LandingPage />,
      },
      {
        path: "login",
        element: <LoginPage />,
      },
      {
        path: "register",
        element: <RegisterPage />,
      },
      {
        path: "privacy",
        element: <PrivacyPage />,
      },
      // Authenticated member routes (Spec 7.1)
      {
        element: <RequireAuth />,
        children: [
          {
            path: "resources",
            element: <ResourceList />,
          },
          {
            path: "resources/:id",
            element: <ResourceDetail enableBooking />,
          },
          {
            path: "my-bookings",
            element: <MyBookingsPage />,
          },
          {
            path: "waitlist",
            element: <WaitlistPage />,
          },
          {
            path: "feedback",
            element: <FeedbackPage />,
          },
        ],
      },
      // Administrator routes (Spec 7.1, 7.2, 8.3)
      {
        path: "admin",
        element: <RequireAuth adminOnly />,
        children: [
          {
            element: <AdminLayout />,
            children: [
              {
                index: true,
                element: <Navigate to="resources" replace />,
              },
              {
                path: "resources",
                element: <AdminResourcesPage />,
              },
              {
                path: "resources/:id/blackouts",
                element: <AdminBlackoutsPage />,
              },
              {
                path: "bookings",
                element: <AdminBookingsPage />,
              },
              {
                path: "users",
                element: <AdminUsersPage />,
              },
              {
                path: "audit",
                element: <AdminAuditPage />,
              },
              {
                path: "outbox",
                element: <AdminOutboxPage />,
              },
              {
                path: "feedback",
                element: <AdminFeedbackPage />,
              },
            ],
          },
        ],
      },
      // Accessible catch-all for not-yet-implemented and unknown routes
      {
        path: "*",
        element: <NotFoundPage />,
      },
    ],
  },
];

export const router = createBrowserRouter(routes);
