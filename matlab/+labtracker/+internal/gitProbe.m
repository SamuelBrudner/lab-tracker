function [ok, output] = gitProbe(root, args)
%GITPROBE Run `git -C root args...` untranslated; never errors.
%
%   ok is true when git exited 0; output is its trimmed output (on failure,
%   git's own message, so a caller can tell "not a git repository" from a
%   real failure, as the Python client's gitinfo.run_git does).
ok = false;
output = '';
try
    quotedArgs = cellfun(@labtracker.internal.shellQuote, args, 'UniformOutput', false);
    command = ['git -C ', labtracker.internal.shellQuote(root), ' ', strjoin(quotedArgs, ' '), ...
        ' 2>&1'];
    if ~ispc
        command = ['LC_ALL=C ', command];
    end
    [status, text] = system(command);
    output = strtrim(text);
    ok = status == 0;
catch err
    output = err.message;
end
end
