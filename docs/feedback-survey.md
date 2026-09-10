# Consented Feedback Survey and Schema Mapping

**Notice Version:** 2026-09-v1  
**Endpoint:** `POST /api/v1/feedback` (Policy: `Policy.authenticated`)  
**Specification:** Spec 4.1, 4.2, 12.2  

---

## 1. Survey Purpose and Governance

The CommonsBook in-app feedback survey captures empirical participant feedback following scheduling and booking interactions. Feedback submission is voluntary, authenticated, and requires affirmative consent acknowledging the versioned Privacy Notice (`docs/privacy.md`).

---

## 2. Question-to-Schema Mapping

The in-app survey form directly maps the questions defined in Spec 12.2 to the `FeedbackCreate` API payload:

| Question # | User-Facing Survey Question | API Field Name | Field Type & Constraints | Database Storage Column |
|---|---|---|---|---|
| **Q1** | *Did you complete your intended task?* | `task_completed` | Boolean (`true` / `false`) | `feedback.task_completed` (boolean, NOT NULL) |
| **Q2** | *How easy was it to use the platform (1 = Very difficult, 5 = Very easy)?* | `rating` | Integer (1..5 inclusive) | `feedback.rating` (smallint, CHECK 1..5) |
| **Q3** | *What was difficult or confusing, if anything?* | `difficulty` | String (0..2000 characters, default empty) | `feedback.difficulty` (text, CHECK length <= 2000) |
| **Q4** | *What is one improvement you would suggest?* | `improvement` | String (0..2000 characters, default empty) | `feedback.improvement` (text, CHECK length <= 2000) |
| **Consent** | *I consent to the collection and processing of this feedback under the Pilot Privacy Notice.* | `consent` | Boolean (strictly `true` required) | Enforced by validation gate |
| **Version** | Pinned consent version | `consent_version` | String (strictly `"2026-09-v1"`) | `feedback.consent_version` (text, CHECK = '2026-09-v1') |

---

## 3. Validation and Error Behavior

1. **Consent Affirmation**:
   - The `consent` field must be explicitly `true`. If omitted or `false`, the submission is rejected with `422 CONSENT_REQUIRED`.
   - The `consent_version` must strictly match the current active version string `"2026-09-v1"`. Any other string returns `422 CONSENT_REQUIRED`.
2. **Field Boundaries**:
   - `rating`: Values outside the range 1 to 5 are rejected with `422 VALIDATION_ERROR`.
   - `difficulty` and `improvement`: Text longer than 2,000 Unicode characters is rejected with `422 VALIDATION_ERROR`.
3. **Rate Limiting**:
   - Submissions consume authenticated mutation rate limits (120 mutations per minute per user) evaluated in a short, independent pre-flight transaction.
4. **Access Protection**:
   - Members can submit feedback via `POST /api/v1/feedback` and receive `201 {id, created_at}`.
   - Members are forbidden (`403 FORBIDDEN`) from querying or reading feedback submitted by other participants.
   - Administrative review of all submitted feedback is available through `GET /api/v1/admin/feedback`.
