function [projectId, source] = captureProject(filePath, explicitProjectId)
%CAPTUREPROJECT The project a saved figure is filed under, and where it came from.
%
%   Same order as the Python client (capture_project.resolve_capture_project):
%   an explicit project ('explicit'), LAB_TRACKER_PROJECT_ID ('environment'),
%   the lt_ids.json of the figure's own git checkout ('checkout'), then that
%   checkout's watch config ('watch_config'). Returns '' when none names one.
projectId = strtrim(labtracker.internal.textOf(explicitProjectId));
source = 'explicit';
if ~isempty(projectId)
    return
end
projectId = strtrim(getenv('LAB_TRACKER_PROJECT_ID'));
source = 'environment';
if ~isempty(projectId)
    return
end
source = '';
try
    folder = fileparts(labtracker.internal.canonicalPath(filePath));
    checkout = labtracker.internal.captureCheckout(folder);
catch
    checkout = '';
end
if isempty(checkout)
    return
end
ids = labtracker.internal.readJsonFile(fullfile(checkout, 'lt_ids.json'));
projectId = labtracker.internal.jsonText(ids, 'project_id');
if ~isempty(projectId)
    source = 'checkout';
    return
end
config = labtracker.internal.watchConfig(checkout);
projectId = config.project_id;
if ~isempty(projectId)
    source = 'watch_config';
end
end
