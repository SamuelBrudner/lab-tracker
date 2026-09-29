function eventPath = queueOffline(filePath, capture, projectId, session, reason)
%QUEUEOFFLINE Queue a capture the server never received as a watch-outbox event.
%
%   eventPath = queueOffline(filePath, capture, projectId, session, reason)
%   writes the event `lt outbox sync` (or a scheduled `lt watch run`) delivers
%   exactly like a figure the Python client queued: the same schema as
%   lab_tracker_client.figure._queue_capture_offline, into the checkout's
%   watch outbox (<checkout>/.lab-tracker/outbox/watch unless
%   LAB_TRACKER_WATCH_OUTBOX or the watch config names another), written
%   atomically. capture carries content_hash, client_capture_id and the
%   capture metadata; session is labtracker.internal.activeSession().
%
%   The event records no mtime: MATLAB cannot reproduce Python's float
%   st_mtime bit for bit, so the sync checks the content hash and size alone.
%
%   Returns '' (never errors) when LAB_TRACKER_CAPTURE_OUTBOX turns queueing
%   off, no project is known, or the outbox cannot be written.
eventPath = '';
try
    if ~outboxEnabled() || isempty(labtracker.internal.textOf(projectId))
        return
    end
    path = labtracker.internal.canonicalPath(filePath);
    [folder, name, ext] = fileparts(path);
    root = labtracker.internal.captureCheckout(folder);
    if isempty(root)
        root = labtracker.internal.canonicalPath(pwd);
    end
    outbox = labtracker.internal.watchOutboxPath(root);
    info = dir(path);
    contentHash = capture.content_hash;
    captureId = capture.client_capture_id;
    eventId = ['figure-', contentHash(1:16)];

    source = struct( ...
        'provider', 'local-figure', ...
        'uri', labtracker.internal.fileUri(path), ...
        'external_id', captureId, ...
        'path', path, ...
        'root', root, ...
        'root_uri', labtracker.internal.fileUri(root), ...
        'relative_path', relativePath(path, root, [name, ext]), ...
        'content_hash', contentHash, ...
        'size_bytes', info.bytes);
    context = struct('project_id', labtracker.internal.textOf(projectId), ...
        'dataset_ids', {{}}, 'tags', {{}});
    if ~isempty(session)
        % The sync re-checks the session against the project the event is
        % filed into, as it does for a Python-queued capture.
        source.session_source = 'active';
        source.session_context = session.source;
        if ~isempty(session.project_id)
            source.session_project_id = session.project_id;
        end
        context.session_id = session.session_id;
    end
    reasonText = labtracker.internal.textOf(reason);
    payload = struct( ...
        'title', [name, ext], ...
        'summary', 'Queued figure capture while Lab Tracker was unreachable.', ...
        'status', 'staged', ...
        'metadata', scalarMetadata(capture.metadata), ...
        'client_capture_id', captureId, ...
        'queue_reason', reasonText(1:min(500, numel(reasonText))));
    event = struct( ...
        'version', 1, ...
        'event_id', eventId, ...
        'capture_id', captureId, ...
        'capture_kind', 'figure', ...
        'adapter', 'lab-tracker-matlab-figure', ...
        'sink', 'staged-note', ...
        'observed_at', labtracker.internal.utcNowIso(), ...
        'source', source, ...
        'context', context, ...
        'artifacts', {{}}, ...
        'metrics', struct(), ...
        'log_excerpt', '', ...
        'payload', payload, ...
        'host', labtracker.internal.hostMetadata(), ...
        'sync', struct('status', 'pending', 'attempts', 0));

    target = fullfile(outbox, [labtracker.internal.safeFilenamePart(captureId), ...
        '.staged-note.', labtracker.internal.safePathPart(eventId), '.json']);
    if exist(target, 'file') ~= 2
        javaObject('java.io.File', outbox).mkdirs();
        labtracker.internal.writeJsonAtomic(target, event);
    end
    eventPath = target;
catch err
    labtracker.internal.warnOnce(['queue-failed:', err.identifier], 'labtracker:queueFailed', ...
        sprintf('Lab Tracker could not queue the figure capture offline: %s', err.message));
    eventPath = '';
end
end

function enabled = outboxEnabled()
value = lower(strtrim(getenv('LAB_TRACKER_CAPTURE_OUTBOX')));
enabled = ~any(strcmp(value, {'0', 'false', 'no', 'off'}));
end

function relative = relativePath(path, root, fallback)
prefix = [root, filesep];
if strncmp(path, prefix, numel(prefix))
    relative = strrep(path(numel(prefix) + 1:end), filesep, '/');
else
    relative = fallback;
end
end

function metadata = scalarMetadata(values)
% Note metadata without the keys the sync writes itself: evidence_* and the
% declared-target label (the sync decides whether the session still targets).
metadata = struct();
if ~isstruct(values)
    return
end
names = fieldnames(values);
for index = 1:numel(names)
    key = names{index};
    value = values.(key);
    scalar = (ischar(value) && (isempty(value) || isrow(value))) || ...
        ((islogical(value) || isnumeric(value)) && isscalar(value));
    owned = strncmp(key, 'evidence_', numel('evidence_')) || ...
        strcmp(key, 'declared_target_source');
    if scalar && ~owned
        metadata.(key) = value;
    end
end
end
