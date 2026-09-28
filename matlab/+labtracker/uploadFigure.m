function result = uploadFigure(filePath, varargin)
%UPLOADFIGURE Capture an existing figure file as a staged Lab Tracker note.
%
%   result = labtracker.uploadFigure("plot.png") uses LAB_TRACKER_* settings.
%   Pass 'Client', client to use an explicit labtracker.Client.
%
%   Fail-soft, like the Python client: it never errors into your script.
%   result.action is 'imported' or 'coalesced' when the server stored the
%   note, 'queued' when the server was unreachable and the capture waits in
%   the checkout's outbox for `lt outbox sync`, and 'skipped' or 'failed'
%   otherwise, each cause warned about once per MATLAB session.
%
%   The project is 'ProjectId', else the client's (LAB_TRACKER_PROJECT_ID),
%   else the figure's checkout lt_ids.json, else its watch config. The
%   active session (LAB_TRACKER_SESSION_ID or `lt session use`) and the git
%   commit, dirty state and remote of MATLAB's current folder are recorded;
%   'RunMetadata', false leaves the git facts out.

parser = inputParser;
parser.addRequired('filePath');
parser.addParameter('Client', []);
parser.addParameter('ProjectId', '');
parser.addParameter('Metadata', struct());
parser.addParameter('LogicalId', '');
parser.addParameter('PreviewMaxBytes', 2000000);
parser.addParameter('VersionEveryChange', false);
parser.addParameter('RunMetadata', true);
parser.parse(filePath, varargin{:});

path = labtracker.internal.textOf(filePath);
client = parser.Results.Client;
if isempty(client)
    client = labtracker.Client.fromEnv();
end

explicitProjectId = labtracker.internal.textOf(parser.Results.ProjectId);
if isempty(explicitProjectId)
    explicitProjectId = labtracker.internal.textOf(client.ProjectId);
end
projectId = labtracker.internal.captureProject(path, explicitProjectId);
session = labtracker.internal.activeSession();
metadata = labtracker.internal.captureMetadata( ...
    parser.Results.Metadata, session, logical(parser.Results.RunMetadata));
targets = '';
sessionTargetId = labtracker.internal.sessionTarget(session, projectId);
if ~isempty(sessionTargetId)
    % A checkout- or shell-wide session is a bounded default, labelled as one.
    targets = sessionTargets(sessionTargetId);
    metadata.declared_target_source = 'config_default';
end

resultDefaults = labtracker.internal.figureResultDefaults( ...
    path, parser.Results.LogicalId, parser.Results.VersionEveryChange, metadata);

if isempty(projectId)
    labtracker.internal.warnOnce('unconfigured', 'labtracker:unconfigured', ...
        ['Lab Tracker MATLAB figure capture is unconfigured; set LAB_TRACKER_PROJECT_ID ', ...
         'or bind the checkout with `lt project bind`.']);
    result = labtracker.internal.mergeStructs(resultDefaults, ...
        struct('action', 'skipped', 'reason', 'unconfigured'));
    return
end

endpoint = labtracker.internal.textOf(client.BaseUrl);
if labtracker.internal.circuitBreaker('blocks', endpoint)
    labtracker.internal.warnOnce(['circuit-open:', endpoint], 'labtracker:circuitOpen', ...
        sprintf(['Lab Tracker figure capture is paused for %s after a connection or ', ...
                 'timeout failure; captures queue offline meanwhile.'], endpoint));
    queued = labtracker.internal.queueOffline(path, resultDefaults, projectId, session, ...
        'circuit_open');
    if isempty(queued)
        result = labtracker.internal.mergeStructs(resultDefaults, ...
            struct('action', 'skipped', 'reason', 'circuit_open'));
    else
        result = labtracker.internal.mergeStructs(resultDefaults, ...
            struct('action', 'queued', 'reason', 'offline_queued', 'queued_event', queued));
    end
    return
end

upload = {'ProjectId', projectId, ...
    'LogicalId', parser.Results.LogicalId, ...
    'PreviewMaxBytes', parser.Results.PreviewMaxBytes, ...
    'VersionEveryChange', parser.Results.VersionEveryChange};
try
    try
        result = client.uploadFigure(path, upload{:}, 'Metadata', metadata, 'Targets', targets);
    catch err
        if isempty(targets) || ~labtracker.internal.isSessionRefusal(err)
            rethrow(err);
        end
        % The server refused the session target: keep the session as plain
        % metadata, file the capture anyway, and stop declaring it here.
        withoutTarget = rmfield(metadata, 'declared_target_source');
        result = client.uploadFigure(path, upload{:}, 'Metadata', withoutTarget);
        labtracker.internal.sessionTarget(session, projectId, 'refused');
        labtracker.internal.warnOnce(['session-target-refused:', sessionTargetId, ':', projectId], ...
            'labtracker:sessionRefused', sprintf(['Lab Tracker: session %s was refused for ', ...
            'project %s (%s); figure captures keep it as plain metadata, not as a session ', ...
            'target.'], sessionTargetId, projectId, err.message));
    end
    labtracker.internal.circuitBreaker('close', endpoint);
catch err
    if labtracker.internal.isTransportFailure(err)
        labtracker.internal.circuitBreaker('trip', endpoint);
        queued = labtracker.internal.queueOffline(path, resultDefaults, projectId, session, ...
            err.message);
        if ~isempty(queued)
            labtracker.internal.warnOnce(['queued:', endpoint], 'labtracker:queued', ...
                sprintf(['Lab Tracker is unreachable (%s); the figure capture was queued in ', ...
                         '%s for a later `lt outbox sync`.'], err.message, fileparts(queued)));
            result = labtracker.internal.mergeStructs(resultDefaults, ...
                struct('action', 'queued', 'reason', 'offline_queued', ...
                       'queued_event', queued, 'error', err.message));
            return
        end
    end
    labtracker.internal.warnOnce(['capture-failed:', err.identifier, ':', err.message], ...
        'labtracker:captureFailed', ...
        sprintf('Lab Tracker MATLAB figure capture failed: %s', err.message));
    result = labtracker.internal.mergeStructs(resultDefaults, ...
        struct('action', 'failed', 'reason', 'capture_failed', 'error', err.message));
end
end

function targets = sessionTargets(sessionId)
targets = sprintf('[{"entity_id": "%s", "entity_type": "session"}]', sessionId);
end
