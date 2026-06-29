function metadata = figureMetadata(path, sourceUri, clientCaptureId, contentHash, sizeBytes, extra)
%FIGUREMETADATA Build scalar evidence metadata for MATLAB figure capture.
if nargin < 6 || isempty(extra)
    metadata = struct();
else
    metadata = extra;
end
metadata.evidence_source_provider = 'local-figure';
metadata.evidence_source_uri = sourceUri;
metadata.evidence_source_external_id = clientCaptureId;
metadata.evidence_source_observed_at = labtracker.internal.iso8601Now();
metadata.evidence_capture_kind = 'figure';
metadata.evidence_content_hash = contentHash;
metadata.evidence_adapter = 'lab-tracker-matlab-figure';
[~, name, ext] = fileparts(path);
metadata.evidence_title = [name, ext];
metadata.content_hash_current = contentHash;
metadata.figure_content_hash_current = contentHash;
metadata.figure_source_uri_current = sourceUri;
metadata.figure_source_external_id_current = clientCaptureId;
metadata.figure_client_capture_id = clientCaptureId;
metadata.figure_full_size_bytes = sizeBytes;
end
