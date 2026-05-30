Here is a piece of script used for processing tables. The coding language is indicated at the beginning of the script.

<start_of_script>
{{ script }}
<end_of_script>

The task is to identify if any of the two columns are joinable to each other according to the given script.
1. If there are multiple pairs of columns that are joinable to each other, output all of them in a list of dictionaries.
2. If there isn't any pair of columns that are joinable to each other, output an empty list.
3. In the output, print the full path to the tables instead of table names only.
4. Please adhere to the following output format, and do not print any other words. Do not include strings like ```json```.

<start_output_format>
[{"file1": "", "column1":"", "file2":"", "column2":""}, ...]
<end_output_format>
