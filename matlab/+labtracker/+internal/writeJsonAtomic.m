function writeJsonAtomic(path, value)
%WRITEJSONATOMIC Write value as UTF-8 JSON through a unique temp file and a rename.
%
%   A reader (`lt outbox sync`) sees either no event file or a complete one:
%   the temp name (.<name>.<uuid>.tmp) is outside the outbox's *.json glob.
[folder, name, ext] = fileparts(path);
token = strrep(char(javaMethod('randomUUID', 'java.util.UUID').toString()), '-', '');
tmpPath = fullfile(folder, ['.', name, ext, '.', token, '.tmp']);
bytes = unicode2native([jsonencode(value), char(10)], 'UTF-8');
fid = fopen(tmpPath, 'w');
if fid < 0
    error('labtracker:io', 'Could not write %s.', tmpPath);
end
try
    fwrite(fid, bytes, 'uint8');
    fclose(fid);
catch err
    fclose(fid);
    delete(tmpPath);
    rethrow(err);
end
[moved, message] = movefile(tmpPath, path, 'f');
if ~moved
    delete(tmpPath);
    error('labtracker:io', 'Could not move %s into place: %s', tmpPath, message);
end
end
