function uri = fileUri(path)
%FILEURI Return a file:// URI for a local path using Java's URI encoder.
file = java.io.File(char(string(path)));
uri = char(file.getCanonicalFile().toURI().toString());
end
