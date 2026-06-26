function result = uploadFigure(filePath, varargin)
%UPLOADFIGURE Capture an existing figure file as a staged Lab Tracker note.
%
%   result = labtracker.uploadFigure("plot.png") uses LAB_TRACKER_* settings.
%   Pass 'Client', client to use an explicit labtracker.Client.

parser = inputParser;
parser.addRequired('filePath');
parser.addParameter('Client', []);
parser.addParameter('ProjectId', '');
parser.addParameter('Metadata', struct());
parser.addParameter('LogicalId', '');
parser.addParameter('PreviewMaxBytes', 2000000);
parser.addParameter('VersionEveryChange', false);
parser.parse(filePath, varargin{:});

client = parser.Results.Client;
if isempty(client)
    client = labtracker.Client.fromEnv();
end

resultDefaults = labtracker.internal.figureResultDefaults( ...
    char(string(filePath)), parser.Results.LogicalId, parser.Results.VersionEveryChange, ...
    parser.Results.Metadata);

projectId = char(string(parser.Results.ProjectId));
if strlength(string(projectId)) == 0
    projectId = client.ProjectId;
end
if strlength(string(projectId)) == 0
    warning('labtracker:unconfigured', ...
        'Lab Tracker MATLAB figure capture is unconfigured; set LAB_TRACKER_PROJECT_ID.');
    result = labtracker.internal.mergeStructs(resultDefaults, ...
        struct('action', 'skipped', 'reason', 'unconfigured'));
    return
end

try
    result = client.uploadFigure( ...
        filePath, ...
        'ProjectId', projectId, ...
        'Metadata', parser.Results.Metadata, ...
        'LogicalId', parser.Results.LogicalId, ...
        'PreviewMaxBytes', parser.Results.PreviewMaxBytes, ...
        'VersionEveryChange', parser.Results.VersionEveryChange);
catch err
    warning('labtracker:captureFailed', ...
        'Lab Tracker MATLAB figure capture failed: %s', err.message);
    result = labtracker.internal.mergeStructs(resultDefaults, ...
        struct('action', 'failed', 'reason', 'capture_failed', 'error', err.message));
end
end
