export type ChatAddAction = {
  id: string;
  label: string;
  description: string;
  run: () => void;
};

export function buildChatAddActions({
  openFilePicker,
}: {
  openFilePicker: () => void;
}): ChatAddAction[] {
  return [
    {
      id: "upload-file",
      label: "Upload file",
      description: "Add an image, table, or document to this chat",
      run: openFilePicker,
    },
  ];
}
