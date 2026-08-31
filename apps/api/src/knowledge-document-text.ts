/**
 * Extract UTF-8 text from a knowledge-base upload.
 * Supports text documents and PDF. Other binary formats are rejected.
 *
 * Gate is extension-whitelist based: MIME alone (including text/*) must not admit .docx etc.
 */
import { extractText, getDocumentProxy } from "unpdf";

const KNOWLEDGE_TEXT_EXTENSIONS = [
  ".txt",
  ".md",
  ".csv",
  ".tsv",
  ".json",
  ".yaml",
  ".yml",
] as const;

const KNOWLEDGE_PDF_EXTENSIONS = [".pdf"] as const;

const hasAllowedKnowledgeTextExtension = (filename: string): boolean => {
  const lower = filename.toLowerCase();
  return KNOWLEDGE_TEXT_EXTENSIONS.some((ext) => lower.endsWith(ext));
};

const hasAllowedKnowledgePdfExtension = (filename: string): boolean => {
  const lower = filename.toLowerCase();
  return KNOWLEDGE_PDF_EXTENSIONS.some((ext) => lower.endsWith(ext));
};

const extractPdfText = async (body: Buffer, filename: string): Promise<string> => {
  try {
    const pdf = await getDocumentProxy(new Uint8Array(body));
    const result = await extractText(pdf, { mergePages: true });
    const content = typeof result.text === "string" ? result.text.trim() : "";
    if (!content) {
      throw new Error(
        `KNOWLEDGE_PDF_EMPTY: No extractable text found in PDF. `
          + `Scanned/image-only PDFs are not supported. Got: ${filename}`,
      );
    }
    return content;
  } catch (error) {
    if (error instanceof Error && error.message.startsWith("KNOWLEDGE_")) {
      throw error;
    }
    throw new Error(
      `KNOWLEDGE_PDF_PARSE_FAILED: Unable to extract text from PDF. Got: ${filename}`
        + (error instanceof Error ? ` (${error.message})` : ""),
    );
  }
};

export const knowledgeDocumentTextFromFile = async (
  filename: string,
  mimeType: string,
  body: Buffer,
): Promise<string> => {
  // mimeType is accepted for callers/logging but must not widen the allowlist.
  void mimeType;

  if (hasAllowedKnowledgePdfExtension(filename)) {
    return extractPdfText(body, filename);
  }

  if (!hasAllowedKnowledgeTextExtension(filename)) {
    throw new Error(
      `KNOWLEDGE_FILE_TYPE_UNSUPPORTED: Only text documents (.txt, .md, .csv, .tsv, .json, .yaml) `
        + `and PDF (.pdf) are supported; Office and other binary formats are not. Got: ${filename}`,
    );
  }

  // Reject mislabeled binaries that would otherwise become mojibake in FTS.
  if (body.includes(0)) {
    throw new Error(
      `KNOWLEDGE_FILE_TYPE_UNSUPPORTED: File looks binary (contains null bytes). `
        + `Only UTF-8 text documents are supported. Got: ${filename}`,
    );
  }
  return body.toString("utf8");
};
