% Lab Tracker MATLAB smoke example.
%
% Set LAB_TRACKER_BASE_URL, LAB_TRACKER_ACCESS_TOKEN or username/password,
% and LAB_TRACKER_PROJECT_ID before running against a live Lab Tracker server.
% This script uses only MATLAB APIs; it does not call Python.

addpath(fileparts(fileparts(mfilename('fullpath'))));

x = linspace(0, 2*pi, 200);
fig = figure('Visible', 'off');
plot(x, sin(x), 'LineWidth', 1.5);
xlabel('radians');
ylabel('sin(x)');
title('Lab Tracker MATLAB capture smoke');

outputPath = fullfile(tempdir, 'lab-tracker-matlab-smoke.png');
result = labtracker.savefig(fig, outputPath, ...
    'Metadata', struct('smoke_test', true, 'source', 'matlab-example'));
disp(result);
