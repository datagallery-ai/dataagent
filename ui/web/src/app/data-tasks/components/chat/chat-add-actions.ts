export type ChatAddAction = {
  id: string;
  label: string;
  description: string;
  run: () => void;
};

export function buildChatAddActions({
  openFilePicker,
  uploadLabel,
  uploadDescription,
}: {
  openFilePicker: () => void;
  uploadLabel: string;
  uploadDescription: string;
}): ChatAddAction[] {
  return [
    {
      id: "upload-file",
      label: uploadLabel,
      description: uploadDescription,
      run: openFilePicker,
    },
  ];
}
