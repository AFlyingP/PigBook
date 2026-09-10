import { createBrowserRouter, RouteObject } from "react-router-dom";
import { Layout } from "./components/Layout";
import { RequireAuth } from "./components/RequireAuth";
import { LandingPage } from "./components/LandingPage";
import { PrivacyPage } from "./components/PrivacyPage";
import { NotFoundPage } from "./components/NotFoundPage";
import { LoginPage } from "./features/auth/LoginPage";
import { RegisterPage } from "./features/auth/RegisterPage";
import { ResourceList } from "./features/resources/ResourceList";
import { ResourceDetail } from "./features/resources/ResourceDetail";

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
      // Authenticated resource routes
      {
        element: <RequireAuth />,
        children: [
          {
            path: "resources",
            element: <ResourceList />,
          },
          {
            path: "resources/:id",
            element: <ResourceDetail />,
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
