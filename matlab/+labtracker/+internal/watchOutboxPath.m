function outbox = watchOutboxPath(checkout)
%WATCHOUTBOXPATH The watch outbox `lt outbox sync` drains for this checkout.
%
%   LAB_TRACKER_WATCH_OUTBOX, else the watch config's outbox, else
%   .lab-tracker/outbox/watch; a relative path is under the checkout, exactly
%   as the Python client's WatchConfig.outbox_path() resolves it.
configured = strtrim(getenv('LAB_TRACKER_WATCH_OUTBOX'));
if isempty(configured)
    config = labtracker.internal.watchConfig(checkout);
    configured = config.outbox;
end
if isempty(configured)
    configured = fullfile('.lab-tracker', 'outbox', 'watch');
end
if strncmp(configured, '~', 1)
    configured = [char(javaMethod('getProperty', 'java.lang.System', 'user.home')), ...
        configured(2:end)];
end
if ~javaObject('java.io.File', configured).isAbsolute()
    configured = fullfile(checkout, configured);
end
outbox = labtracker.internal.canonicalPath(configured);
end
