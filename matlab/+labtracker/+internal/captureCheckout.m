function root = captureCheckout(folder)
%CAPTURECHECKOUT The git checkout root containing folder, or '' outside any checkout.
root = '';
try
    [ok, output] = labtracker.internal.gitProbe(folder, {'rev-parse', '--show-toplevel'});
    if ok && ~isempty(output)
        lines = strsplit(output, char(10));
        root = labtracker.internal.canonicalPath(strtrim(lines{end}));
    end
catch
    root = '';
end
end
