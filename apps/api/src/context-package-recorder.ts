import type { ContextPackageRecorder } from "@datafoundry/agent-runtime";
import type { MetadataStore } from "@datafoundry/metadata";

export const createMetadataContextPackageRecorder = (input: {
  metadataStore: MetadataStore;
  runId: string;
  sessionId: string;
  userId: string;
}): ContextPackageRecorder => ({
  record: ({ contextPackage, plan }) => {
    input.metadataStore.contextPackageSnapshots.create({
      user_id: input.userId,
      session_id: input.sessionId,
      run_id: input.runId,
      package_id: contextPackage.packageId,
      revision: contextPackage.revision,
      payload: contextPackage,
      plan
    });
  }
});
