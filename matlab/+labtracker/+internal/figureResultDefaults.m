function defaults = figureResultDefaults(filePath, logicalId, versionEveryChange, extraMetadata)
%FIGURERESULTDEFAULTS Best-effort result fields used for fail-soft wrappers.
defaults = struct( ...
    'action', 'failed', ...
    'path', labtracker.internal.textOf(filePath), ...
    'source_external_id', '', ...
    'source_uri', '', ...
    'content_hash', '', ...
    'metadata', struct(), ...
    'client_capture_id', '', ...
    'no_preview', false);
try
    path = labtracker.internal.textOf(filePath);
    info = dir(path);
    if isempty(info) || info.bytes <= 0
        return
    end
    contentHash = labtracker.internal.sha256File(path);
    sourceUri = labtracker.internal.fileUri(path);
    resolvedLogicalId = labtracker.internal.textOf(logicalId);
    if isempty(resolvedLogicalId)
        resolvedLogicalId = labtracker.internal.logicalId(path);
    end
    if versionEveryChange
        captureId = labtracker.internal.clientCaptureId(resolvedLogicalId, contentHash);
    else
        captureId = labtracker.internal.clientCaptureId(resolvedLogicalId);
    end
    metadata = labtracker.internal.figureMetadata( ...
        path, sourceUri, captureId, contentHash, info.bytes, extraMetadata);
    defaults.source_external_id = captureId;
    defaults.source_uri = sourceUri;
    defaults.content_hash = contentHash;
    defaults.metadata = metadata;
    defaults.client_capture_id = captureId;
catch
end
end
