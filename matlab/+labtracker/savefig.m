function result = savefig(fig, filePath, varargin)
%SAVEFIG Save a MATLAB figure, then capture it as staged Lab Tracker evidence.
%
%   result = labtracker.savefig(gcf, "plot.png") writes the figure and uploads
%   a staged evidence note with source URI, content hash, and client_capture_id.

parser = inputParser;
parser.addRequired('fig');
parser.addRequired('filePath');
parser.addParameter('Client', []);
parser.addParameter('ProjectId', '');
parser.addParameter('Metadata', struct());
parser.addParameter('LogicalId', '');
parser.addParameter('PreviewMaxBytes', 2000000);
parser.addParameter('VersionEveryChange', false);
parser.addParameter('Resolution', 150);
parser.parse(fig, filePath, varargin{:});

target = char(string(filePath));
parent = fileparts(target);
if strlength(string(parent)) > 0 && ~exist(parent, 'dir')
    mkdir(parent);
end

try
    exportgraphics(fig, target, 'Resolution', parser.Results.Resolution);
catch
    saveas(fig, target);
end

result = labtracker.uploadFigure( ...
    target, ...
    'Client', parser.Results.Client, ...
    'ProjectId', parser.Results.ProjectId, ...
    'Metadata', parser.Results.Metadata, ...
    'LogicalId', parser.Results.LogicalId, ...
    'PreviewMaxBytes', parser.Results.PreviewMaxBytes, ...
    'VersionEveryChange', parser.Results.VersionEveryChange);
end
