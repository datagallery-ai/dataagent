import assert from "node:assert/strict";
import { knowledgeDocumentTextFromFile } from "../apps/api/dist/knowledge-document-text.js";

assert.equal(
  await knowledgeDocumentTextFromFile("notes.md", "text/markdown", Buffer.from("# hi\n", "utf8")),
  "# hi\n",
);
assert.equal(
  await knowledgeDocumentTextFromFile("rows.csv", "text/csv", Buffer.from("a,b\n1,2\n", "utf8")),
  "a,b\n1,2\n",
);
// Extension whitelist wins even when MIME is empty / generic.
assert.equal(
  await knowledgeDocumentTextFromFile("notes.txt", "application/octet-stream", Buffer.from("plain\n", "utf8")),
  "plain\n",
);

let invalidPdfError;
try {
  await knowledgeDocumentTextFromFile("doc.pdf", "application/pdf", Buffer.from("%PDF-1.4", "utf8"));
} catch (error) {
  invalidPdfError = error;
}
assert(invalidPdfError instanceof Error, "invalid PDF must fail parse/empty checks");
assert.match(invalidPdfError.message, /KNOWLEDGE_PDF_(EMPTY|PARSE_FAILED)/);

let forgedMimeError;
try {
  // Spoofed text MIME still routes by .pdf extension into the PDF extractor.
  await knowledgeDocumentTextFromFile("doc.pdf", "text/plain", Buffer.from("not really a pdf", "utf8"));
} catch (error) {
  forgedMimeError = error;
}
assert(forgedMimeError instanceof Error, "forged text/plain PDF must fail PDF extraction");
assert.match(forgedMimeError.message, /KNOWLEDGE_PDF_(EMPTY|PARSE_FAILED)/);

let docxError;
try {
  await knowledgeDocumentTextFromFile(
    "report.docx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    Buffer.from("PK", "utf8"),
  );
} catch (error) {
  docxError = error;
}
assert(docxError instanceof Error, "DOCX must be rejected");
assert.match(docxError.message, /KNOWLEDGE_FILE_TYPE_UNSUPPORTED/);
assert.match(docxError.message, /PDF \(\.pdf\) are supported/);

let binaryError;
try {
  await knowledgeDocumentTextFromFile("fake.txt", "text/plain", Buffer.from([0x00, 0x01, 0x02]));
} catch (error) {
  binaryError = error;
}
assert(binaryError instanceof Error, "null-byte body must be rejected");
assert.match(binaryError.message, /null bytes/);

console.log("knowledge-document-text smoke OK");
