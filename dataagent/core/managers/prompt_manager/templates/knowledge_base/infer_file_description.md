Here is a piece of document explaining a business logic of a related table with file name {{ filename }}:

<start_of_document>
{{ document }}
<end_of_document>

And here are columns of the .csv file:
<start_of_columns>
{{ columns }}
<end_of_columns>

Currently I have a piece of file description:
<start_of_description>
{{ file_description }}
<end_of_description>

The task is to refine the above file description with the given document. Here are specific instructions:
1. If the document contains information about this table, summarize it and refine the given file description in English;
2. If the document does not contain information about this table, and the given file description is not empty, just return the given file description;
3. If the document does not contain information about this table, and the given file description is empty, please infer the file description from its file name and columns;
4. Keep it short and simple and do not describe the meaning of each column;
5. Organize your response into one paragraph. Do not change lines. Do not include any headers or subtitles.
