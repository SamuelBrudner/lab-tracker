function metadata = hostMetadata()
%HOSTMETADATA Which machine made the capture: capture_host_label and capture_platform.
%
%   LAB_TRACKER_CAPTURE_HOST, else the hostname, and the OS family as Python's
%   platform.system() names it. The Python client's per-install id and
%   release are not stamped: those judge a Python client install, not MATLAB.
persistent hostname
metadata = struct();
label = strtrim(getenv('LAB_TRACKER_CAPTURE_HOST'));
if isempty(label)
    if isempty(hostname)
        hostname = '';
        try
            [status, output] = system('hostname');
            if status == 0
                hostname = strtrim(output);
            end
        catch
            hostname = '';
        end
    end
    label = hostname;
end
if ~isempty(label)
    metadata.capture_host_label = label;
end
if ispc
    metadata.capture_platform = 'Windows';
elseif ismac
    metadata.capture_platform = 'Darwin';
else
    metadata.capture_platform = 'Linux';
end
end
