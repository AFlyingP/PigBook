import React, { useState } from "react";
import {
  Box,
  Typography,
  TextField,
  FormControlLabel,
  Checkbox,
  Button,
  Alert,
  Paper,
  CircularProgress,
  RadioGroup,
  Radio,
  FormControl,
  FormLabel,
  Rating,
  Link,
} from "@mui/material";
import { Link as RouterLink } from "react-router-dom";
import { request, ApiError } from "../../api/client";
import type { components } from "../../api/schema";

type FeedbackCreate = components["schemas"]["FeedbackCreate"];
type FeedbackCreateResult = components["schemas"]["FeedbackCreateResult"];

export const CONSENT_VERSION = "2026-09-v1";

export function FeedbackForm() {
  // Controlled form state
  const [rating, setRating] = useState<number | null>(5);
  const [taskCompleted, setTaskCompleted] = useState<boolean>(true);
  const [difficulty, setDifficulty] = useState<string>("");
  const [improvement, setImprovement] = useState<string>("");
  // SPEC REQUIREMENT: Consent checkbox must be unchecked on first render!
  const [consent, setConsent] = useState<boolean>(false);

  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  const [submitted, setSubmitted] = useState<boolean>(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [consentError, setConsentError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState<string>("");

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErrorMsg(null);
    setConsentError(null);

    // Validate consent before sending or allow server to validate
    if (!consent) {
      setConsentError("Consent is required to submit survey responses. Please review the consent notice below.");
      return;
    }

    if (!rating || rating < 1 || rating > 5) {
      setErrorMsg("Please select an overall experience rating (1 to 5 stars).");
      return;
    }

    setIsSubmitting(true);

    const payload: FeedbackCreate = {
      rating,
      task_completed: taskCompleted,
      difficulty: difficulty.trim(),
      improvement: improvement.trim(),
      consent_version: CONSENT_VERSION,
      consent: true,
    };

    try {
      await request<FeedbackCreateResult>("/api/v1/feedback", {
        method: "POST",
        body: payload,
      });

      setSubmitted(true);
      setAnnouncement("Thank you! Your feedback has been submitted successfully.");
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 422 && err.code === "CONSENT_REQUIRED") {
          // Spec: On 422 CONSENT_REQUIRED show consent requirement inline without losing entered text
          setConsentError("Affirmative consent is required to participate in the survey (CONSENT_REQUIRED).");
        } else {
          setErrorMsg(err.message || "Failed to submit feedback.");
        }
      } else {
        setErrorMsg("A network or unexpected error occurred.");
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleReset = () => {
    setRating(5);
    setTaskCompleted(true);
    setDifficulty("");
    setImprovement("");
    setConsent(false);
    setSubmitted(false);
    setErrorMsg(null);
    setConsentError(null);
    setAnnouncement("");
  };

  if (submitted) {
    return (
      <Paper variant="outlined" sx={{ p: { xs: 2, sm: 4 }, maxWidth: 650, mx: "auto", textAlign: "center" }}>
        {/* Screen reader live region */}
        <Box role="status" aria-live="polite" sx={{ position: "absolute", width: "1px", height: "1px", overflow: "hidden" }}>
          {announcement}
        </Box>

        <Alert severity="success" sx={{ mb: 3 }} role="alert">
          Feedback Submitted Successfully!
        </Alert>
        <Typography variant="body1" paragraph>
          Thank you for taking the time to share your feedback. Your input directly helps improve CommonsBook.
        </Typography>
        <Button variant="outlined" onClick={handleReset} sx={{ mt: 2 }}>
          Submit Another Response
        </Button>
      </Paper>
    );
  }

  return (
    <Paper variant="outlined" sx={{ p: { xs: 2, sm: 4 }, maxWidth: 650, mx: "auto" }}>
      {/* Screen reader live region */}
      <Box role="status" aria-live="polite" sx={{ position: "absolute", width: "1px", height: "1px", overflow: "hidden" }}>
        {announcement}
      </Box>

      <Typography variant="h5" component="h2" fontWeight="bold" gutterBottom>
        Community Feedback Survey
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Please help us improve CommonsBook by answering four brief questions about your reservation experience.
      </Typography>

      {errorMsg && (
        <Alert severity="error" sx={{ mb: 3 }} role="alert">
          {errorMsg}
        </Alert>
      )}

      <form onSubmit={handleSubmit} noValidate>
        <Box sx={{ display: "flex", flexDirection: "column", gap: 3 }}>
          {/* Question 1: Rating (1..5) */}
          <FormControl component="fieldset">
            <FormLabel component="legend" id="feedback-rating-label" sx={{ fontWeight: "medium", color: "text.primary", mb: 1 }}>
              1. How was your overall experience using CommonsBook? (1 = Poor, 5 = Excellent)
            </FormLabel>
            <Box sx={{ display: "flex", alignItems: "center", gap: 2, flexWrap: "wrap" }}>
              <Rating
                name="experience-rating"
                id="feedback-rating"
                value={rating}
                onChange={(_, newValue) => setRating(newValue)}
                size="large"
                aria-labelledby="feedback-rating-label"
                emptyLabelText="No rating selected"
              />
              <Typography variant="body2" color="text.secondary">
                {rating ? `${rating} of 5 stars` : "Select rating"}
              </Typography>
            </Box>
          </FormControl>

          {/* Question 2: Task completed (boolean) */}
          <FormControl component="fieldset">
            <FormLabel component="legend" id="feedback-task-completed-label" sx={{ fontWeight: "medium", color: "text.primary", mb: 1 }}>
              2. Were you able to complete the reservation task you intended to do?
            </FormLabel>
            <RadioGroup
              row
              aria-labelledby="feedback-task-completed-label"
              name="task-completed-group"
              value={taskCompleted ? "yes" : "no"}
              onChange={(e) => setTaskCompleted(e.target.value === "yes")}
            >
              <FormControlLabel value="yes" control={<Radio id="task-completed-yes" />} label="Yes" />
              <FormControlLabel value="no" control={<Radio id="task-completed-no" />} label="No" />
            </RadioGroup>
          </FormControl>

          {/* Question 3: Difficulty (0..2000) */}
          <Box>
            <Typography component="label" htmlFor="feedback-difficulty" variant="body2" fontWeight="medium" display="block" gutterBottom>
              3. What, if anything, was difficult or confusing? (Optional)
            </Typography>
            <TextField
              id="feedback-difficulty"
              multiline
              rows={3}
              fullWidth
              value={difficulty}
              onChange={(e) => setDifficulty(e.target.value)}
              inputProps={{ maxLength: 2000 }}
              helperText="Up to 2000 characters"
              disabled={isSubmitting}
            />
          </Box>

          {/* Question 4: Improvement (0..2000) */}
          <Box>
            <Typography component="label" htmlFor="feedback-improvement" variant="body2" fontWeight="medium" display="block" gutterBottom>
              4. What is one improvement or feature you would suggest? (Optional)
            </Typography>
            <TextField
              id="feedback-improvement"
              multiline
              rows={3}
              fullWidth
              value={improvement}
              onChange={(e) => setImprovement(e.target.value)}
              inputProps={{ maxLength: 2000 }}
              helperText="Up to 2000 characters"
              disabled={isSubmitting}
            />
          </Box>

          {/* Consent Checkbox */}
          <Box sx={{ pt: 1, borderTop: "1px solid #e0e0e0" }}>
            {consentError && (
              <Alert severity="error" sx={{ mb: 2 }} role="alert" aria-live="polite">
                {consentError}
              </Alert>
            )}

            <FormControlLabel
              sx={{ mr: 0, width: "100%", alignItems: "flex-start" }}
              control={
                <Checkbox
                  id="feedback-consent"
                  checked={consent}
                  onChange={(e) => {
                    setConsent(e.target.checked);
                    if (e.target.checked) setConsentError(null);
                  }}
                  color="primary"
                  disabled={isSubmitting}
                  inputProps={{
                    "aria-describedby": "consent-description",
                  }}
                />
              }
              label={
                <Typography variant="body2" sx={{ mt: 1 }}>
                  I consent to having my survey responses recorded for operating and improving CommonsBook ({CONSENT_VERSION}).
                </Typography>
              }
            />

            <Typography variant="caption" color="text.secondary" id="consent-description" display="block" sx={{ mt: 0.5, ml: 4 }}>
              Your feedback is stored securely and processed in accordance with our{" "}
              <Link component={RouterLink} to="/privacy" target="_blank" rel="noopener noreferrer">
                Privacy Notice
              </Link>
              . Responses are never sold or used for marketing.
            </Typography>
          </Box>

          <Button
            type="submit"
            variant="contained"
            color="primary"
            size="large"
            disabled={isSubmitting}
            startIcon={isSubmitting ? <CircularProgress size={20} /> : null}
            id="submit-feedback-btn"
          >
            {isSubmitting ? "Submitting..." : "Submit Feedback"}
          </Button>
        </Box>
      </form>
    </Paper>
  );
}
