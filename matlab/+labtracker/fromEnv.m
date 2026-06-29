function client = fromEnv()
%FROMENV Build a Lab Tracker MATLAB client from LAB_TRACKER_* variables.
client = labtracker.Client.fromEnv();
end
