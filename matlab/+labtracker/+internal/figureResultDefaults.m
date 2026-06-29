function defaults = figureResultDefaults(filePath, logicalId, versionEveryChange, extraMetadata)
%FIGURERESULTDEFAULTS Best-effort result fields used for fail-soft wrappers.
defaults = struct( ...
    'action', 'failed', ...
    'path', char(string(filePath)), ...
    'source_external_id', '', ...
    'source_uri', '', ...
    'content_hash', '', ...
    'metadata', struct(), ...
    'client_capture_id', '', ...
    'no_preview', false);
try
    path = char(string(filePath));
    info = dir(path);
    if isempty(info) || info.bytes <= 0
        return
    end
    contentHash = labtracker.internal.sha256File(path);
    sourceUri = labtracker.internal.fileUri(path);
    resolvedLogicalId = char(string(logicalId));
    if strlength(string(resolvedLogicalId)) == 0
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
