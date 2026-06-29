classdef Client < handle
    %CLIENT Minimal Lab Tracker API client for MATLAB analysis scripts.
    %
    %   client = labtracker.Client.fromEnv() reads LAB_TRACKER_BASE_URL,
    %   LAB_TRACKER_ACCESS_TOKEN, LAB_TRACKER_USERNAME, LAB_TRACKER_PASSWORD,
    %   and LAB_TRACKER_PROJECT_ID. The client talks directly to the Lab
    %   Tracker HTTP API; it does not require Python.

    properties
        BaseUrl
        AccessToken
        Username
        Password
        ProjectId
        Timeout
    end

    methods (Static)
        function client = fromEnv()
            baseUrl = getenv('LAB_TRACKER_BASE_URL');
            if strlength(string(baseUrl)) == 0
                baseUrl = getenv('LAB_TRACKER_MCP_BASE_URL');
            end
            if strlength(string(baseUrl)) == 0
                baseUrl = 'http://127.0.0.1:8000';
            end

            timeoutText = getenv('LAB_TRACKER_HTTP_TIMEOUT');
            timeout = 15;
            if strlength(string(timeoutText)) > 0
                parsed = str2double(timeoutText);
                if ~isnan(parsed) && parsed > 0
                    timeout = parsed;
                end
            end

            username = getenv('LAB_TRACKER_USERNAME');
            if strlength(string(username)) == 0
                username = getenv('LAB_TRACKER_MCP_USERNAME');
            end
            password = getenv('LAB_TRACKER_PASSWORD');
            if strlength(string(password)) == 0
                password = getenv('LAB_TRACKER_MCP_PASSWORD');
            end

            client = labtracker.Client( ...
                'BaseUrl', baseUrl, ...
                'AccessToken', getenv('LAB_TRACKER_ACCESS_TOKEN'), ...
                'Username', username, ...
                'Password', password, ...
                'ProjectId', getenv('LAB_TRACKER_PROJECT_ID'), ...
                'Timeout', timeout);
        end
    end

    methods
        function obj = Client(varargin)
            parser = inputParser;
            parser.addParameter('BaseUrl', 'http://127.0.0.1:8000');
            parser.addParameter('AccessToken', '');
            parser.addParameter('Username', '');
            parser.addParameter('Password', '');
            parser.addParameter('ProjectId', '');
            parser.addParameter('Timeout', 15);
            parser.parse(varargin{:});

            baseUrl = char(string(parser.Results.BaseUrl));
            while ~isempty(baseUrl) && baseUrl(end) == '/'
                baseUrl(end) = [];
            end
            obj.BaseUrl = baseUrl;
            obj.AccessToken = char(string(parser.Results.AccessToken));
            obj.Username = char(string(parser.Results.Username));
            obj.Password = char(string(parser.Results.Password));
            obj.ProjectId = char(string(parser.Results.ProjectId));
            obj.Timeout = double(parser.Results.Timeout);
        end

        function payload = health(obj)
            payload = obj.request('GET', '/health', struct(), false);
        end

        function token = login(obj)
            if strlength(string(obj.Username)) == 0 || strlength(string(obj.Password)) == 0
                error('labtracker:auth', ...
                    ['LAB_TRACKER_USERNAME and LAB_TRACKER_PASSWORD are required ', ...
                     'when no LAB_TRACKER_ACCESS_TOKEN is configured.']);
            end
            body = struct('username', obj.Username, 'password', obj.Password);
            payload = obj.request('POST', '/auth/login', body, false);
            if ~isfield(payload, 'data') || ~isfield(payload.data, 'access_token')
                error('labtracker:api', 'Login response did not include data.access_token.');
            end
            obj.AccessToken = char(string(payload.data.access_token));
            token = obj.AccessToken;
        end

        function [note, statusCode] = uploadNoteFile(obj, filePath, varargin)
            parser = inputParser;
            parser.addRequired('filePath');
            parser.addParameter('ProjectId', obj.ProjectId);
            parser.addParameter('Metadata', struct());
            parser.addParameter('Status', 'staged');
            parser.addParameter('ClientCaptureId', '');
            parser.addParameter('TranscribedText', '');
            parser.parse(filePath, varargin{:});

            resolvedProjectId = char(string(parser.Results.ProjectId));
            if strlength(string(resolvedProjectId)) == 0
                error('labtracker:validation', ...
                    'ProjectId is required, or set LAB_TRACKER_PROJECT_ID.');
            end

            path = char(string(filePath));
            info = dir(path);
            if isempty(info) || info.bytes <= 0
                error('labtracker:validation', ...
                    'filePath must point to a non-empty file.');
            end

            fields = struct();
            fields.project_id = resolvedProjectId;
            fields.status = char(string(parser.Results.Status));
            if ~isempty(fieldnames(parser.Results.Metadata))
                fields.metadata = jsonencode(parser.Results.Metadata);
            end
            if strlength(string(parser.Results.ClientCaptureId)) > 0
                fields.client_capture_id = char(string(parser.Results.ClientCaptureId));
            end
            if strlength(string(parser.Results.TranscribedText)) > 0
                fields.transcribed_text = char(string(parser.Results.TranscribedText));
            end

            [payload, statusCode] = obj.requestMultipart('/notes/upload-file', fields, path);
            if ~isfield(payload, 'data')
                error('labtracker:api', ...
                    'Lab Tracker API response did not include a data field.');
            end
            note = payload.data;
        end

        function result = uploadFigure(obj, filePath, varargin)
            parser = inputParser;
            parser.addRequired('filePath');
            parser.addParameter('ProjectId', obj.ProjectId);
            parser.addParameter('Metadata', struct());
            parser.addParameter('LogicalId', '');
            parser.addParameter('PreviewMaxBytes', 2000000);
            parser.addParameter('VersionEveryChange', false);
            parser.parse(filePath, varargin{:});

            path = char(string(filePath));
            info = dir(path);
            if isempty(info) || info.bytes <= 0
                error('labtracker:validation', ...
                    'filePath must point to a non-empty figure file.');
            end

            contentHash = labtracker.internal.sha256File(path);
            sourceUri = labtracker.internal.fileUri(path);
            logicalId = char(string(parser.Results.LogicalId));
            if strlength(string(logicalId)) == 0
                logicalId = labtracker.internal.logicalId(path);
            end
            if parser.Results.VersionEveryChange
                clientCaptureId = labtracker.internal.clientCaptureId(logicalId, contentHash);
            else
                clientCaptureId = labtracker.internal.clientCaptureId(logicalId);
            end

            metadata = labtracker.internal.figureMetadata( ...
                path, sourceUri, clientCaptureId, contentHash, info.bytes, ...
                parser.Results.Metadata);

            cleanup = [];
            uploadPath = path;
            previewMaxBytes = max(1, min(double(parser.Results.PreviewMaxBytes), 100 * 1024 * 1024));
            noPreview = false;
            if info.bytes > previewMaxBytes
                noPreview = true;
                uploadPath = [tempname, '.pointer.txt'];
                pointerText = sprintf(['Lab Tracker figure pointer\n', ...
                    'source_uri: %s\n', ...
                    'evidence_content_hash: %s\n', ...
                    'full_size_bytes: %d\n'], sourceUri, contentHash, info.bytes);
                fid = fopen(uploadPath, 'w');
                if fid < 0
                    error('labtracker:io', 'Could not write temporary pointer note.');
                end
                closer = onCleanup(@() fclose(fid));
                fwrite(fid, pointerText);
                clear closer;
                cleanup = onCleanup(@() delete(uploadPath));
            end

            uploadInfo = dir(uploadPath);
            metadata.figure_no_preview = noPreview;
            metadata.figure_preview_size_bytes = uploadInfo.bytes;
            metadata.figure_review_bytes_stale = false;

            [note, statusCode] = obj.uploadNoteFile( ...
                uploadPath, ...
                'ProjectId', parser.Results.ProjectId, ...
                'Metadata', metadata, ...
                'Status', 'staged', ...
                'ClientCaptureId', clientCaptureId);

            if ~isempty(cleanup)
                clear cleanup;
            end

            action = 'imported';
            if statusCode == 200
                action = 'coalesced';
            end
            result = struct( ...
                'action', action, ...
                'path', path, ...
                'source_external_id', clientCaptureId, ...
                'source_uri', sourceUri, ...
                'content_hash', contentHash, ...
                'metadata', metadata, ...
                'client_capture_id', clientCaptureId, ...
                'note', note, ...
                'no_preview', noPreview);
        end

        function payload = request(obj, method, path, body, authenticated)
            if nargin < 5
                authenticated = true;
            end
            [payload, ~] = obj.requestJson(method, path, body, authenticated);
        end
    end

    methods (Access = private)
        function [payload, statusCode] = requestJson(obj, method, path, body, authenticated)
            import matlab.net.URI
            import matlab.net.http.HTTPOptions
            import matlab.net.http.HeaderField
            import matlab.net.http.MessageBody
            import matlab.net.http.RequestMessage

            headers = obj.headers(authenticated);
            if nargin >= 4 && ~isempty(body) && ~strcmpi(method, 'GET')
                headers(end + 1) = HeaderField('Content-Type', 'application/json');
                messageBody = MessageBody(jsonencode(body));
            else
                messageBody = [];
            end

            request = RequestMessage(labtracker.Client.requestMethod(method), headers, messageBody);
            response = request.send(URI([obj.BaseUrl, char(path)]), ...
                HTTPOptions('ConnectTimeout', obj.Timeout));
            [payload, statusCode] = obj.decodeResponse(response);

            if statusCode == 401 && authenticated && strlength(string(obj.Username)) > 0
                obj.AccessToken = '';
                obj.login();
                headers = obj.headers(true);
                if nargin >= 4 && ~isempty(body) && ~strcmpi(method, 'GET')
                    headers(end + 1) = HeaderField('Content-Type', 'application/json');
                end
                request = RequestMessage(labtracker.Client.requestMethod(method), headers, messageBody);
                response = request.send(URI([obj.BaseUrl, char(path)]), ...
                    HTTPOptions('ConnectTimeout', obj.Timeout));
                [payload, statusCode] = obj.decodeResponse(response);
            end

            obj.raiseForStatus(statusCode, payload);
        end

        function [payload, statusCode] = requestMultipart(obj, path, fields, filePath)
            import matlab.net.URI
            import matlab.net.http.HTTPOptions
            import matlab.net.http.RequestMessage
            import matlab.net.http.io.FileProvider
            import matlab.net.http.io.MultipartFormProvider
            import matlab.net.http.io.StringProvider

            names = fieldnames(fields);
            parts = {};
            for idx = 1:numel(names)
                value = fields.(names{idx});
                if strlength(string(value)) > 0
                    parts(end + 1:end + 2) = {names{idx}, StringProvider(char(string(value)))}; %#ok<AGROW>
                end
            end
            parts(end + 1:end + 2) = {'file', FileProvider(char(filePath))};

            request = RequestMessage(matlab.net.http.RequestMethod.POST, ...
                obj.headers(true), MultipartFormProvider(parts{:}));
            response = request.send(URI([obj.BaseUrl, char(path)]), ...
                HTTPOptions('ConnectTimeout', obj.Timeout));
            [payload, statusCode] = obj.decodeResponse(response);

            if statusCode == 401 && strlength(string(obj.Username)) > 0
                obj.AccessToken = '';
                obj.login();
                request = RequestMessage(matlab.net.http.RequestMethod.POST, ...
                    obj.headers(true), MultipartFormProvider(parts{:}));
                response = request.send(URI([obj.BaseUrl, char(path)]), ...
                    HTTPOptions('ConnectTimeout', obj.Timeout));
                [payload, statusCode] = obj.decodeResponse(response);
            end

            obj.raiseForStatus(statusCode, payload);
        end

        function headers = headers(obj, authenticated)
            import matlab.net.http.HeaderField

            headers = HeaderField('Accept', 'application/json');
            headers(end + 1) = HeaderField('X-LabTracker-Surface', 'matlab');
            if authenticated
                if strlength(string(obj.AccessToken)) == 0 && strlength(string(obj.Username)) > 0
                    obj.login();
                end
                if strlength(string(obj.AccessToken)) > 0
                    headers(end + 1) = HeaderField('Authorization', ...
                        ['Bearer ', char(obj.AccessToken)]);
                end
            end
        end
    end

    methods (Static, Access = private)
        function [payload, statusCode] = decodeResponse(response)
            statusCode = double(response.StatusCode);
            data = response.Body.Data;
            if isempty(data)
                payload = struct();
                return
            end
            if isstruct(data)
                payload = data;
                return
            end
            if isa(data, 'uint8') || isa(data, 'int8')
                text = char(data(:).');
            else
                text = char(string(data));
            end
            if strlength(string(strtrim(text))) == 0
                payload = struct();
            else
                payload = jsondecode(text);
            end
        end

        function raiseForStatus(statusCode, payload)
            if statusCode < 400
                return
            end
            message = sprintf('Lab Tracker API returned HTTP %d.', statusCode);
            if isstruct(payload) && isfield(payload, 'error') && isfield(payload.error, 'message')
                message = char(string(payload.error.message));
            end
            if statusCode == 422
                error('labtracker:validation', '%s', message);
            end
            error('labtracker:api', '%s', message);
        end

        function methodValue = requestMethod(method)
            switch upper(char(method))
                case 'GET'
                    methodValue = matlab.net.http.RequestMethod.GET;
                case 'POST'
                    methodValue = matlab.net.http.RequestMethod.POST;
                case 'PATCH'
                    methodValue = matlab.net.http.RequestMethod.PATCH;
                otherwise
                    error('labtracker:validation', ...
                        'Unsupported HTTP method for MATLAB client: %s', char(method));
            end
        end
    end
end
