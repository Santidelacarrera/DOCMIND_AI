import { api } from "./api";

export type AnswerFeedbackPayload = {
  question: string;
  answer: string;
  rating: "positive" | "negative";
  corrected_answer?: string;
  project_id?: string | null;
  conversation_id?: string | null;
  turn_seq?: number | null;
  cited_document_ids: string[];
};

export async function submitAnswerFeedback(
  organizationId: string,
  payload: AnswerFeedbackPayload,
): Promise<boolean> {
  const res = await api(
    `/api/v1/feedback?organization_id=${encodeURIComponent(organizationId)}`,
    { method: "POST", body: JSON.stringify(payload) },
  );
  return res.ok;
}
