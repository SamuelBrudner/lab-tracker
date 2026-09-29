function metadata = runMetadata(root)
%RUNMETADATA Git run facts for a capture, under the Python client's run_* keys.
%
%   run_captured_at, then run_git_commit (or run_git_commit_error when git
%   could not say), run_git_dirty (or run_git_status_error: an unknown state
%   is never reported as clean) and run_repo_remote_url (credential-free).
%   root defaults to MATLAB's current folder, where the analysis code runs.
%   Outside a git checkout there is no commit and run_git_dirty is false.
if nargin < 1 || isempty(root)
    root = pwd;
end
metadata = struct();
metadata.run_captured_at = labtracker.internal.utcNowIso();

[ok, output] = labtracker.internal.gitProbe(root, {'rev-parse', 'HEAD'});
commit = '';
commitError = '';
if ok
    commit = output;
elseif isempty(regexp(output, ...
        'not a git repository|ambiguous argument ''HEAD'': unknown revision', 'once'))
    commitError = ['git rev-parse HEAD failed: ', output];
end

[ok, output] = labtracker.internal.gitProbe(root, {'status', '--porcelain'});
if ok
    metadata.run_git_dirty = ~isempty(output);
elseif isempty(commit) && isempty(commitError)
    metadata.run_git_dirty = false;
else
    metadata.run_git_status_error = ['git status --porcelain failed: ', output];
end

if ~isempty(commit)
    metadata.run_git_commit = commit;
elseif ~isempty(commitError)
    metadata.run_git_commit_error = commitError;
end

[ok, output] = labtracker.internal.gitProbe(root, {'config', '--get', 'remote.origin.url'});
if ok
    remote = labtracker.internal.credentialFreeRemote(output);
    if ~isempty(remote)
        metadata.run_repo_remote_url = remote;
    end
end
end
