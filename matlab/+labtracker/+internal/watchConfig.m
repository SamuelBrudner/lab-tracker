function config = watchConfig(checkout)
%WATCHCONFIG The checkout's watch config fields that capture needs (project_id, outbox).
%
%   LAB_TRACKER_WATCH_CONFIG names an explicit config file; otherwise it is
%   <checkout>/.lab-tracker/watch.json. A missing or broken file yields ''.
config = struct('project_id', '', 'outbox', '');
explicit = strtrim(getenv('LAB_TRACKER_WATCH_CONFIG'));
if ~isempty(explicit)
    path = explicit;
elseif ~isempty(checkout)
    path = fullfile(checkout, '.lab-tracker', 'watch.json');
else
    return
end
payload = labtracker.internal.readJsonFile(path);
config.project_id = labtracker.internal.jsonText(payload, 'project_id');
config.outbox = labtracker.internal.jsonText(payload, 'outbox');
end
