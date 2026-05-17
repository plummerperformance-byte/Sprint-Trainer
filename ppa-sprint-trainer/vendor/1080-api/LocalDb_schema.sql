-- 1080 Trainitest LocalDb.sqlite — schema only (no data)
-- 112 objects

CREATE TABLE "ActiveSession" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_ActiveSession" PRIMARY KEY,
    "InstructorGUID" TEXT NOT NULL,
    "SessionState" INTEGER NOT NULL,
    "RowVersion" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "IsLocallyEdited" INTEGER NOT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "CreatedOffset" INTEGER NOT NULL,
    "EditedUTC" INTEGER NULL,
    "EditedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "DeletedOffset" INTEGER NULL, "CompletedOffset" INTEGER NULL, "CompletedUTC" INTEGER NULL, "PlannedStartOffset" TEXT NULL, "PlannedStartUTC" TEXT NULL, "Title" TEXT NULL,
    CONSTRAINT "FK_ActiveSession_Instructor_InstructorGUID" FOREIGN KEY ("InstructorGUID") REFERENCES "Instructor" ("GUID") ON DELETE CASCADE
);

CREATE TABLE "ActiveSessionExercise" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_ActiveSessionExercise" PRIMARY KEY,
    "ActiveSessionGUID" TEXT NOT NULL,
    "ExerciseTypeGUID" TEXT NOT NULL,
    "RowVersion" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "IsLocallyEdited" INTEGER NOT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "CreatedOffset" INTEGER NOT NULL,
    "EditedUTC" INTEGER NULL,
    "EditedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "DeletedOffset" INTEGER NULL,
    CONSTRAINT "FK_ActiveSessionExercise_ActiveSession_ActiveSessionGUID" FOREIGN KEY ("ActiveSessionGUID") REFERENCES "ActiveSession" ("GUID") ON DELETE CASCADE,
    CONSTRAINT "FK_ActiveSessionExercise_ExerciseType_ExerciseTypeGUID" FOREIGN KEY ("ExerciseTypeGUID") REFERENCES "ExerciseType" ("GUID") ON DELETE CASCADE
);

CREATE TABLE "ActiveSessionParticipant" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_ActiveSessionParticipant" PRIMARY KEY,
    "ActiveSessionGUID" TEXT NOT NULL,
    "ClientGUID" TEXT NOT NULL,
    "RowVersion" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "IsLocallyEdited" INTEGER NOT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "CreatedOffset" INTEGER NOT NULL,
    "EditedUTC" INTEGER NULL,
    "EditedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "DeletedOffset" INTEGER NULL, "GroupAssignment" TEXT NULL,
    CONSTRAINT "FK_ActiveSessionParticipant_ActiveSession_ActiveSessionGUID" FOREIGN KEY ("ActiveSessionGUID") REFERENCES "ActiveSession" ("GUID") ON DELETE CASCADE,
    CONSTRAINT "FK_ActiveSessionParticipant_Client_ClientGUID" FOREIGN KEY ("ClientGUID") REFERENCES "Client" ("GUID") ON DELETE CASCADE
);

CREATE TABLE "ActiveSessionResult" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_ActiveSessionResult" PRIMARY KEY,
    "ActiveSessionGUID" TEXT NOT NULL,
    "ClientGUID" TEXT NOT NULL,
    "CreatedOffset" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "DeletedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "EditedOffset" INTEGER NULL,
    "EditedUTC" INTEGER NULL,
    "ExerciseTypeGUID" TEXT NOT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "IsLocallyEdited" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "ResultData" BLOB NULL,
    "RowVersion" INTEGER NOT NULL,
    CONSTRAINT "FK_ActiveSessionResult_ActiveSession_ActiveSessionGUID" FOREIGN KEY ("ActiveSessionGUID") REFERENCES "ActiveSession" ("GUID") ON DELETE CASCADE,
    CONSTRAINT "FK_ActiveSessionResult_Client_ClientGUID" FOREIGN KEY ("ClientGUID") REFERENCES "Client" ("GUID") ON DELETE CASCADE,
    CONSTRAINT "FK_ActiveSessionResult_ExerciseType_ExerciseTypeGUID" FOREIGN KEY ("ExerciseTypeGUID") REFERENCES "ExerciseType" ("GUID") ON DELETE CASCADE
);

CREATE TABLE "ActiveSessionTarget" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_ActiveSessionTarget" PRIMARY KEY,
    "ActiveSessionGUID" TEXT NOT NULL,
    "ClientGUID" TEXT NOT NULL,
    "ExerciseTypeGUID" TEXT NOT NULL,
    "TargetData" TEXT NULL,
    "RowVersion" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "IsLocallyEdited" INTEGER NOT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "CreatedOffset" INTEGER NOT NULL,
    "EditedUTC" INTEGER NULL,
    "EditedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "DeletedOffset" INTEGER NULL,
    CONSTRAINT "FK_ActiveSessionTarget_ActiveSession_ActiveSessionGUID" FOREIGN KEY ("ActiveSessionGUID") REFERENCES "ActiveSession" ("GUID") ON DELETE CASCADE
);

CREATE TABLE "Client" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_Client" PRIMARY KEY,
    "DisplayName" TEXT NULL,
    "FirstName" TEXT NULL,
    "LastName" TEXT NULL,
    "DateOfBirth" TEXT NULL,
    "IsMale" INTEGER NULL,
    "Length" REAL NOT NULL,
    "Weight" REAL NOT NULL,
    "MailAdress" TEXT NULL,
    "InstructorGUID" TEXT NOT NULL,
    "LastActivated" TEXT NULL,
    "RowVersion" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "IsLocallyEdited" INTEGER NOT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "CreatedOffset" INTEGER NOT NULL,
    "EditedUTC" INTEGER NULL,
    "EditedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "DeletedOffset" INTEGER NULL, "ExternalId" TEXT NULL, "Tags" TEXT NULL, "ImageBytes" BLOB NULL,
    CONSTRAINT "FK_Client_Instructor_InstructorGUID" FOREIGN KEY ("InstructorGUID") REFERENCES "Instructor" ("GUID") ON DELETE RESTRICT
);

CREATE TABLE "ClientMeasurement" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_ClientMeasurement" PRIMARY KEY,
    "ClientGUID" TEXT NOT NULL,
    "EntryDate" TEXT NOT NULL,
    "Length" REAL NOT NULL,
    "Weight" REAL NOT NULL,
    "RowVersion" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "IsLocallyEdited" INTEGER NOT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "CreatedOffset" INTEGER NOT NULL,
    "EditedUTC" INTEGER NULL,
    "EditedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "DeletedOffset" INTEGER NULL,
    CONSTRAINT "FK_ClientMeasurement_Client_ClientGUID" FOREIGN KEY ("ClientGUID") REFERENCES "Client" ("GUID") ON DELETE CASCADE
);

CREATE TABLE "ClientPerformanceMeasurement" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_ClientPerformanceMeasurement" PRIMARY KEY,
    "ClientGUID" TEXT NOT NULL,
    "ExerciseTypeGUID" TEXT NULL,
    "ExerciseArchTypeGUID" TEXT NULL,
    "MeasurementDate" TEXT NOT NULL,
    "Payload" TEXT NULL,
    "RowVersion" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "IsLocallyEdited" INTEGER NOT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "CreatedOffset" INTEGER NOT NULL,
    "EditedUTC" INTEGER NULL,
    "EditedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "DeletedOffset" INTEGER NULL, "Side" INTEGER NULL,
    CONSTRAINT "FK_ClientPerformanceMeasurement_Client_ClientGUID" FOREIGN KEY ("ClientGUID") REFERENCES "Client" ("GUID") ON DELETE CASCADE
);

CREATE TABLE "ControlDevice" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_ControlDevice" PRIMARY KEY,
    "Name" TEXT NOT NULL,
    "Type" TEXT NULL,
    "UniqueGUID" TEXT NULL,
    "LastOnline" TEXT NOT NULL,
    "TeamViewerID" TEXT NULL,
    "TeamViewerName" TEXT NULL,
    "SoftwareVersion" TEXT NULL,
    "Comment" TEXT NULL,
    "WindowsKey" TEXT NULL,
    "RowVersion" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "IsLocallyEdited" INTEGER NOT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "CreatedOffset" INTEGER NOT NULL,
    "EditedUTC" INTEGER NULL,
    "EditedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "DeletedOffset" INTEGER NULL
);

CREATE TABLE "Exercise" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_Exercise" PRIMARY KEY,
    "Notes" TEXT NULL,
    "IsTest" INTEGER NOT NULL,
    "SessionGUID" TEXT NOT NULL,
    "ExerciseTypeGUID" TEXT NOT NULL,
    "SortIndex" INTEGER NOT NULL,
    "RowVersion" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "IsLocallyEdited" INTEGER NOT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "CreatedOffset" INTEGER NOT NULL,
    "EditedUTC" INTEGER NULL,
    "EditedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "DeletedOffset" INTEGER NULL,
    CONSTRAINT "FK_Exercise_ExerciseType_ExerciseTypeGUID" FOREIGN KEY ("ExerciseTypeGUID") REFERENCES "ExerciseType" ("GUID") ON DELETE CASCADE,
    CONSTRAINT "FK_Exercise_Session_SessionGUID" FOREIGN KEY ("SessionGUID") REFERENCES "Session" ("GUID") ON DELETE CASCADE
);

CREATE TABLE "ExerciseArchType" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_ExerciseArchType" PRIMARY KEY,
    "Name" TEXT NULL,
    "FriendlyName" TEXT NULL,
    "PresentationType" INTEGER NOT NULL,
    "RowVersion" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "IsLocallyEdited" INTEGER NOT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "CreatedOffset" INTEGER NOT NULL,
    "EditedUTC" INTEGER NULL,
    "EditedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "DeletedOffset" INTEGER NULL
);

CREATE TABLE "ExerciseType" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_ExerciseType" PRIMARY KEY,
    "CreatedOffset" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "DeletedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "EditedOffset" INTEGER NULL,
    "EditedUTC" INTEGER NULL,
    "ExerciseArchTypeGUID" TEXT NOT NULL,
    "FlyingDistance" REAL NULL,
    "Hyperlink" TEXT NULL,
    "Instructions" TEXT NULL,
    "InstructorGUID" TEXT NULL,
    "IsBilateral" INTEGER NOT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "IsLocallyEdited" INTEGER NOT NULL,
    "IsStartWithConcentric" INTEGER NOT NULL,
    "IsTest" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "MachineType" INTEGER NOT NULL,
    "Name" TEXT NOT NULL,
    "NumOfTurns" INTEGER NULL,
    "RowVersion" INTEGER NOT NULL,
    "Settings" TEXT NULL,
    "StartImageBytes" BLOB NULL,
    "StopImageBytes" BLOB NULL,
    "Stroke" REAL NULL,
    "Tags" TEXT NULL,
    CONSTRAINT "FK_ExerciseType_ExerciseArchType_ExerciseArchTypeGUID" FOREIGN KEY ("ExerciseArchTypeGUID") REFERENCES "ExerciseArchType" ("GUID") ON DELETE CASCADE,
    CONSTRAINT "FK_ExerciseType_Instructor_InstructorGUID" FOREIGN KEY ("InstructorGUID") REFERENCES "Instructor" ("GUID") ON DELETE CASCADE
);

CREATE TABLE "Group" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_Group" PRIMARY KEY,
    "CreatedOffset" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "DeletedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "EditedOffset" INTEGER NULL,
    "EditedUTC" INTEGER NULL,
    "InstructorGUID" TEXT NOT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "IsLocallyEdited" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "Name" TEXT NOT NULL,
    "RowVersion" INTEGER NOT NULL,
    CONSTRAINT "FK_Group_Instructor_InstructorGUID" FOREIGN KEY ("InstructorGUID") REFERENCES "Instructor" ("GUID") ON DELETE CASCADE
);

CREATE TABLE "GroupSession" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_GroupSession" PRIMARY KEY,
    "CreatedOffset" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "DeletedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "EditedOffset" INTEGER NULL,
    "EditedUTC" INTEGER NULL,
    "InstructorGUID" TEXT NOT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "IsLocallyEdited" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "RowVersion" INTEGER NOT NULL,
    "Title" TEXT NULL,
    CONSTRAINT "FK_GroupSession_Instructor_InstructorGUID" FOREIGN KEY ("InstructorGUID") REFERENCES "Instructor" ("GUID") ON DELETE CASCADE
);

CREATE TABLE "GroupXClient" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_GroupXClient" PRIMARY KEY,
    "GroupGUID" TEXT NOT NULL,
    "ClientGUID" TEXT NOT NULL,
    "RowVersion" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "IsLocallyEdited" INTEGER NOT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "CreatedOffset" INTEGER NOT NULL,
    "EditedUTC" INTEGER NULL,
    "EditedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "DeletedOffset" INTEGER NULL,
    CONSTRAINT "FK_GroupXClient_Client_ClientGUID" FOREIGN KEY ("ClientGUID") REFERENCES "Client" ("GUID") ON DELETE CASCADE,
    CONSTRAINT "FK_GroupXClient_Group_GroupGUID" FOREIGN KEY ("GroupGUID") REFERENCES "Group" ("GUID") ON DELETE CASCADE
);

CREATE TABLE "Instructor" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_Instructor" PRIMARY KEY,
    "CreatedOffset" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "DeletedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "DisplayName" TEXT NULL,
    "EditedOffset" INTEGER NULL,
    "EditedUTC" INTEGER NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "IsLocallyEdited" INTEGER NOT NULL,
    "KindeOrganizationId" TEXT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "MailAdress" TEXT NULL,
    "RowVersion" INTEGER NOT NULL,
    "UseSetData" INTEGER NOT NULL,
    "Username" TEXT NOT NULL
);

CREATE TABLE "InstructorSettings" (
    "InstructorId" TEXT NOT NULL CONSTRAINT "PK_InstructorSettings" PRIMARY KEY,
    "LocalVersion" TEXT NULL,
    "ServerVersion" TEXT NULL
);

CREATE TABLE "Machine" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_Machine" PRIMARY KEY,
    "SerialNo" INTEGER NOT NULL,
    "Name" TEXT NOT NULL,
    "Location" TEXT NULL,
    "Comment" TEXT NULL,
    "PLCMacAdress" TEXT NOT NULL,
    "Chassi" INTEGER NOT NULL,
    "CurrentVersion" TEXT NULL,
    "MachineType" INTEGER NOT NULL,
    "IsLightFlywheel" INTEGER NOT NULL,
    "NodeNo" INTEGER NOT NULL,
    "RowVersion" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "IsLocallyEdited" INTEGER NOT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "CreatedOffset" INTEGER NOT NULL,
    "EditedUTC" INTEGER NULL,
    "EditedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "DeletedOffset" INTEGER NULL
, "SerialNo2" TEXT NULL);

CREATE TABLE "Motion" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_Motion" PRIMARY KEY,
    "IsEccentric" INTEGER NOT NULL,
    "StartIndex" INTEGER NULL,
    "StopIndex" INTEGER NULL,
    "NodeNo" INTEGER NOT NULL,
    "ConMass" REAL NOT NULL,
    "EccMass" REAL NOT NULL,
    "Mode" INTEGER NOT NULL,
    "Gear" INTEGER NOT NULL,
    "ConSpeed" REAL NOT NULL,
    "EccSpeed" REAL NOT NULL,
    "IsBoostActive" INTEGER NOT NULL,
    "BoostLoad" REAL NOT NULL,
    "MassAtV0" REAL NULL,
    "MassAtV1" REAL NULL,
    "SpeedV1" REAL NULL,
    "MachineGUID" TEXT NOT NULL,
    "PeakSpeed" REAL NULL,
    "AvgSpeed" REAL NULL,
    "PeakForce" REAL NULL,
    "AvgForce" REAL NULL,
    "PeakPower" REAL NULL,
    "AvgPower" REAL NULL,
    "PeakAcceleration" REAL NULL,
    "AvgAcceleration" REAL NULL,
    "StartPosition" REAL NULL,
    "StopPosition" REAL NULL,
    "Work" REAL NULL,
    "TotalDuration" REAL NULL,
    "Data" BLOB NOT NULL,
    "Distance" REAL NULL,
    "Duration" REAL NULL,
    "TrimType" INTEGER NOT NULL,
    "MotionGroupGUID" TEXT NOT NULL,
    "RowVersion" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "IsLocallyEdited" INTEGER NOT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "CreatedOffset" INTEGER NOT NULL,
    "EditedUTC" INTEGER NULL,
    "EditedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "DeletedOffset" INTEGER NULL, "TotalDistance" REAL NULL, "TopSpeed" REAL NULL,
    CONSTRAINT "FK_Motion_Machine_MachineGUID" FOREIGN KEY ("MachineGUID") REFERENCES "Machine" ("GUID") ON DELETE CASCADE,
    CONSTRAINT "FK_Motion_MotionGroup_MotionGroupGUID" FOREIGN KEY ("MotionGroupGUID") REFERENCES "MotionGroup" ("GUID") ON DELETE CASCADE
);

CREATE TABLE "MotionGroup" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_MotionGroup" PRIMARY KEY,
    "SetGUID" TEXT NOT NULL,
    "IsCurveSelected" INTEGER NOT NULL,
    "Comment" TEXT NULL,
    "IsFvSelected" INTEGER NOT NULL,
    "Side" INTEGER NOT NULL,
    "ColorRGB" TEXT NULL,
    "RowVersion" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "IsLocallyEdited" INTEGER NOT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "CreatedOffset" INTEGER NOT NULL,
    "EditedUTC" INTEGER NULL,
    "EditedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "DeletedOffset" INTEGER NULL,
    CONSTRAINT "FK_MotionGroup_Set_SetGUID" FOREIGN KEY ("SetGUID") REFERENCES "Set" ("GUID") ON DELETE CASCADE
);

CREATE TABLE "Protocol" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_Protocol" PRIMARY KEY,
    "CreatedOffset" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "DeletedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "Description" TEXT NULL,
    "EditedOffset" INTEGER NULL,
    "EditedUTC" INTEGER NULL,
    "ImageData" BLOB NULL,
    "InstructorGUID" TEXT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "IsLocallyEdited" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "Name" TEXT NULL,
    "RowVersion" INTEGER NOT NULL,
    "Summary" TEXT NULL,
    CONSTRAINT "FK_Protocol_Instructor_InstructorGUID" FOREIGN KEY ("InstructorGUID") REFERENCES "Instructor" ("GUID")
);

CREATE TABLE "ProtocolXExerciseType" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_ProtocolXExerciseType" PRIMARY KEY,
    "ProtocolGUID" TEXT NOT NULL,
    "ExerciseTypeGUID" TEXT NOT NULL,
    "RowVersion" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "IsLocallyEdited" INTEGER NOT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "CreatedOffset" INTEGER NOT NULL,
    "EditedUTC" INTEGER NULL,
    "EditedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "DeletedOffset" INTEGER NULL,
    CONSTRAINT "FK_ProtocolXExerciseType_ExerciseType_ExerciseTypeGUID" FOREIGN KEY ("ExerciseTypeGUID") REFERENCES "ExerciseType" ("GUID") ON DELETE CASCADE,
    CONSTRAINT "FK_ProtocolXExerciseType_Protocol_ProtocolGUID" FOREIGN KEY ("ProtocolGUID") REFERENCES "Protocol" ("GUID") ON DELETE CASCADE
);

CREATE TABLE "SavedAccount" (
    "Id" INTEGER NOT NULL CONSTRAINT "PK_SavedAccount" PRIMARY KEY AUTOINCREMENT,
    "IdentityProvider" INTEGER NOT NULL,
    "UserIdentifier" TEXT COLLATE NOCASE NOT NULL,
    "Handle" TEXT COLLATE NOCASE NOT NULL,
    "DisplayName" TEXT NULL,
    "RefreshToken" TEXT NULL,
    "PasswordHash" TEXT NULL,
    "Picture" TEXT NULL,
    "RefreshTokenExpiry" TEXT NOT NULL,
    "Permissions" INTEGER NOT NULL,
    "ExperimentalFeatures" TEXT NULL,
    "LastLogin" TEXT NOT NULL,
    "InstructorGUID" TEXT NULL,
    CONSTRAINT "FK_SavedAccount_Instructor_InstructorGUID" FOREIGN KEY ("InstructorGUID") REFERENCES "Instructor" ("GUID")
);

CREATE TABLE "Session" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_Session" PRIMARY KEY,
    "ClientGUID" TEXT NOT NULL,
    "CreatedOffset" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "DeletedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "EditedOffset" INTEGER NULL,
    "EditedUTC" INTEGER NULL,
    "GroupSessionGUID" TEXT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "IsLocallyEdited" INTEGER NOT NULL,
    "IsTest" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "RowVersion" INTEGER NOT NULL,
    "Title" TEXT NULL,
    CONSTRAINT "FK_Session_Client_ClientGUID" FOREIGN KEY ("ClientGUID") REFERENCES "Client" ("GUID") ON DELETE CASCADE,
    CONSTRAINT "FK_Session_GroupSession_GroupSessionGUID" FOREIGN KEY ("GroupSessionGUID") REFERENCES "GroupSession" ("GUID") ON DELETE SET NULL
);

CREATE TABLE "Set" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_Set" PRIMARY KEY,
    "BargraphVisibility" INTEGER NOT NULL,
    "IsAverageVisible" INTEGER NOT NULL,
    "IsSplitBarSumOrAvgActive" INTEGER NOT NULL,
    "ExternalLoad" INTEGER NOT NULL,
    "ExerciseGUID" TEXT NOT NULL,
    "Phase" INTEGER NULL,
    "Unit" INTEGER NOT NULL,
    "UnitX" INTEGER NULL,
    "ChildrenRowVersionSum" INTEGER NOT NULL,
    "RowVersion" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "IsLocallyEdited" INTEGER NOT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "CreatedOffset" INTEGER NOT NULL,
    "EditedUTC" INTEGER NULL,
    "EditedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "DeletedOffset" INTEGER NULL,
    CONSTRAINT "FK_Set_Exercise_ExerciseGUID" FOREIGN KEY ("ExerciseGUID") REFERENCES "Exercise" ("GUID") ON DELETE CASCADE
);

CREATE TABLE "SetData" (
    "GUID" TEXT NOT NULL CONSTRAINT "PK_SetData" PRIMARY KEY,
    "SetGUID" TEXT NOT NULL,
    "Data" BLOB NULL,
    "RowVersion" INTEGER NOT NULL,
    "LocalDbRowVersion" INTEGER NOT NULL DEFAULT 0,
    "IsLocallyEdited" INTEGER NOT NULL,
    "IsLocallyDeleted" INTEGER NOT NULL,
    "CreatedUTC" INTEGER NOT NULL,
    "CreatedOffset" INTEGER NOT NULL,
    "EditedUTC" INTEGER NULL,
    "EditedOffset" INTEGER NULL,
    "DeletedUTC" INTEGER NULL,
    "DeletedOffset" INTEGER NULL,
    CONSTRAINT "FK_SetData_Set_SetGUID" FOREIGN KEY ("SetGUID") REFERENCES "Set" ("GUID") ON DELETE CASCADE
);

CREATE TABLE "Users" (
    "Id" INTEGER NOT NULL CONSTRAINT "PK_Users" PRIMARY KEY AUTOINCREMENT,
    "Username" TEXT NOT NULL,
    "PasswordHash" TEXT NOT NULL,
    "Permissions" INTEGER NOT NULL,
    "LastLogin" TEXT NOT NULL,
    "InstructorGUID" TEXT NOT NULL, "RefreshToken" TEXT NULL, "RefreshTokenExpiry" TEXT NOT NULL DEFAULT '0001-01-01 00:00:00', "DisplayName" TEXT NULL, "ExperimentalFeatures" TEXT NULL, "Email" TEXT NULL, "EmailVerified" INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT "FK_Users_Instructor_InstructorGUID" FOREIGN KEY ("InstructorGUID") REFERENCES "Instructor" ("GUID") ON DELETE CASCADE
);

CREATE TABLE "__EFMigrationsHistory" (
    "MigrationId" TEXT NOT NULL CONSTRAINT "PK___EFMigrationsHistory" PRIMARY KEY,
    "ProductVersion" TEXT NOT NULL
);

CREATE TABLE "__EFMigrationsLock" (
    "Id" INTEGER NOT NULL CONSTRAINT "PK___EFMigrationsLock" PRIMARY KEY,
    "Timestamp" TEXT NOT NULL
);

CREATE TABLE sqlite_sequence(name,seq);

CREATE INDEX "IX_ActiveSessionExercise_ActiveSessionGUID" ON "ActiveSessionExercise" ("ActiveSessionGUID");

CREATE INDEX "IX_ActiveSessionExercise_ExerciseTypeGUID" ON "ActiveSessionExercise" ("ExerciseTypeGUID");

CREATE INDEX "IX_ActiveSessionParticipant_ActiveSessionGUID" ON "ActiveSessionParticipant" ("ActiveSessionGUID");

CREATE INDEX "IX_ActiveSessionParticipant_ClientGUID" ON "ActiveSessionParticipant" ("ClientGUID");

CREATE INDEX "IX_ActiveSessionResult_ActiveSessionGUID" ON "ActiveSessionResult" ("ActiveSessionGUID");

CREATE INDEX "IX_ActiveSessionResult_ClientGUID" ON "ActiveSessionResult" ("ClientGUID");

CREATE INDEX "IX_ActiveSessionResult_ExerciseTypeGUID" ON "ActiveSessionResult" ("ExerciseTypeGUID");

CREATE INDEX "IX_ActiveSessionTarget_ActiveSessionGUID" ON "ActiveSessionTarget" ("ActiveSessionGUID");

CREATE INDEX "IX_ActiveSession_InstructorGUID" ON "ActiveSession" ("InstructorGUID");

CREATE INDEX "IX_ClientMeasurement_ClientGUID" ON "ClientMeasurement" ("ClientGUID");

CREATE INDEX "IX_ClientPerformanceMeasurement_ClientGUID" ON "ClientPerformanceMeasurement" ("ClientGUID");

CREATE INDEX "IX_Client_InstructorGUID" ON "Client" ("InstructorGUID");

CREATE INDEX "IX_ExerciseType_ExerciseArchTypeGUID" ON "ExerciseType" ("ExerciseArchTypeGUID");

CREATE INDEX "IX_ExerciseType_InstructorGUID" ON "ExerciseType" ("InstructorGUID");

CREATE INDEX "IX_Exercise_ExerciseTypeGUID" ON "Exercise" ("ExerciseTypeGUID");

CREATE INDEX "IX_Exercise_SessionGUID" ON "Exercise" ("SessionGUID");

CREATE INDEX "IX_GroupSession_InstructorGUID" ON "GroupSession" ("InstructorGUID");

CREATE INDEX "IX_GroupXClient_ClientGUID" ON "GroupXClient" ("ClientGUID");

CREATE INDEX "IX_GroupXClient_GroupGUID" ON "GroupXClient" ("GroupGUID");

CREATE INDEX "IX_Group_InstructorGUID" ON "Group" ("InstructorGUID");

CREATE INDEX "IX_MotionGroup_SetGUID" ON "MotionGroup" ("SetGUID");

CREATE INDEX "IX_Motion_MachineGUID" ON "Motion" ("MachineGUID");

CREATE INDEX "IX_Motion_MotionGroupGUID" ON "Motion" ("MotionGroupGUID");

CREATE INDEX "IX_ProtocolXExerciseType_ExerciseTypeGUID" ON "ProtocolXExerciseType" ("ExerciseTypeGUID");

CREATE INDEX "IX_ProtocolXExerciseType_ProtocolGUID" ON "ProtocolXExerciseType" ("ProtocolGUID");

CREATE INDEX "IX_Protocol_InstructorGUID" ON "Protocol" ("InstructorGUID");

CREATE INDEX "IX_SavedAccount_InstructorGUID" ON "SavedAccount" ("InstructorGUID");

CREATE UNIQUE INDEX "IX_SavedAccount_UserIdentifier_IdentityProvider" ON "SavedAccount" ("UserIdentifier", "IdentityProvider");

CREATE INDEX "IX_Session_ClientGUID" ON "Session" ("ClientGUID");

CREATE INDEX "IX_Session_GroupSessionGUID" ON "Session" ("GroupSessionGUID");

CREATE UNIQUE INDEX "IX_SetData_SetGUID" ON "SetData" ("SetGUID");

CREATE INDEX "IX_Set_ExerciseGUID" ON "Set" ("ExerciseGUID");

CREATE INDEX "IX_Users_InstructorGUID" ON "Users" ("InstructorGUID");

CREATE UNIQUE INDEX "IX_Users_Username" ON "Users" ("Username");

CREATE TRIGGER [SetActiveSessionExerciseRowVersionINSERT]
    AFTER INSERT ON [ActiveSessionExercise]
    BEGIN
        UPDATE [ActiveSessionExercise]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetActiveSessionExerciseRowVersionUPDATE]
    AFTER UPDATE ON [ActiveSessionExercise]
    BEGIN
        UPDATE [ActiveSessionExercise]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetActiveSessionParticipantRowVersionINSERT]
    AFTER INSERT ON [ActiveSessionParticipant]
    BEGIN
        UPDATE [ActiveSessionParticipant]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetActiveSessionParticipantRowVersionUPDATE]
    AFTER UPDATE ON [ActiveSessionParticipant]
    BEGIN
        UPDATE [ActiveSessionParticipant]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetActiveSessionResultRowVersionINSERT]
    AFTER INSERT ON [ActiveSessionResult]
    BEGIN
        UPDATE [ActiveSessionResult]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetActiveSessionResultRowVersionUPDATE]
    AFTER UPDATE ON [ActiveSessionResult]
    BEGIN
        UPDATE [ActiveSessionResult]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetActiveSessionRowVersionINSERT]
    AFTER INSERT ON [ActiveSession]
    BEGIN
        UPDATE [ActiveSession]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetActiveSessionRowVersionUPDATE]
    AFTER UPDATE ON [ActiveSession]
    BEGIN
        UPDATE [ActiveSession]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetActiveSessionTargetRowVersionINSERT]
    AFTER INSERT ON [ActiveSessionTarget]
    BEGIN
        UPDATE [ActiveSessionTarget]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetActiveSessionTargetRowVersionUPDATE]
    AFTER UPDATE ON [ActiveSessionTarget]
    BEGIN
        UPDATE [ActiveSessionTarget]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetClientMeasurementRowVersionINSERT]
    AFTER INSERT ON [ClientMeasurement]
    BEGIN
        UPDATE [ClientMeasurement]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetClientMeasurementRowVersionUPDATE]
    AFTER UPDATE ON [ClientMeasurement]
    BEGIN
        UPDATE [ClientMeasurement]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetClientPerformanceMeasurementRowVersionINSERT]
    AFTER INSERT ON [ClientPerformanceMeasurement]
    BEGIN
        UPDATE [ClientPerformanceMeasurement]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetClientPerformanceMeasurementRowVersionUPDATE]
    AFTER UPDATE ON [ClientPerformanceMeasurement]
    BEGIN
        UPDATE [ClientPerformanceMeasurement]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetClientRowVersionINSERT]
    AFTER INSERT ON [Client]
    BEGIN
        UPDATE [Client]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetClientRowVersionUPDATE]
    AFTER UPDATE ON [Client]
    BEGIN
        UPDATE [Client]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetControlDeviceRowVersionINSERT]
    AFTER INSERT ON [ControlDevice]
    BEGIN
        UPDATE [ControlDevice]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetControlDeviceRowVersionUPDATE]
    AFTER UPDATE ON [ControlDevice]
    BEGIN
        UPDATE [ControlDevice]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetExerciseArchTypeRowVersionINSERT]
    AFTER INSERT ON [ExerciseArchType]
    BEGIN
        UPDATE [ExerciseArchType]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetExerciseArchTypeRowVersionUPDATE]
    AFTER UPDATE ON [ExerciseArchType]
    BEGIN
        UPDATE [ExerciseArchType]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetExerciseRowVersionINSERT]
    AFTER INSERT ON [Exercise]
    BEGIN
        UPDATE [Exercise]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetExerciseRowVersionUPDATE]
    AFTER UPDATE ON [Exercise]
    BEGIN
        UPDATE [Exercise]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetExerciseTypeRowVersionINSERT]
    AFTER INSERT ON [ExerciseType]
    BEGIN
        UPDATE [ExerciseType]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetExerciseTypeRowVersionUPDATE]
    AFTER UPDATE ON [ExerciseType]
    BEGIN
        UPDATE [ExerciseType]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetGroupRowVersionINSERT]
    AFTER INSERT ON [Group]
    BEGIN
        UPDATE [Group]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetGroupRowVersionUPDATE]
    AFTER UPDATE ON [Group]
    BEGIN
        UPDATE [Group]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetGroupSessionRowVersionINSERT]
    AFTER INSERT ON [GroupSession]
    BEGIN
        UPDATE [GroupSession]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetGroupSessionRowVersionUPDATE]
    AFTER UPDATE ON [GroupSession]
    BEGIN
        UPDATE [GroupSession]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetGroupXClientRowVersionINSERT]
    AFTER INSERT ON [GroupXClient]
    BEGIN
        UPDATE [GroupXClient]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetGroupXClientRowVersionUPDATE]
    AFTER UPDATE ON [GroupXClient]
    BEGIN
        UPDATE [GroupXClient]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetInstructorRowVersionINSERT]
    AFTER INSERT ON [Instructor]
    BEGIN
        UPDATE [Instructor]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetInstructorRowVersionUPDATE]
    AFTER UPDATE ON [Instructor]
    BEGIN
        UPDATE [Instructor]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetMachineRowVersionINSERT]
    AFTER INSERT ON [Machine]
    BEGIN
        UPDATE [Machine]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetMachineRowVersionUPDATE]
    AFTER UPDATE ON [Machine]
    BEGIN
        UPDATE [Machine]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetMotionGroupRowVersionINSERT]
    AFTER INSERT ON [MotionGroup]
    BEGIN
        UPDATE [MotionGroup]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetMotionGroupRowVersionUPDATE]
    AFTER UPDATE ON [MotionGroup]
    BEGIN
        UPDATE [MotionGroup]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetMotionRowVersionINSERT]
    AFTER INSERT ON [Motion]
    BEGIN
        UPDATE [Motion]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetMotionRowVersionUPDATE]
    AFTER UPDATE ON [Motion]
    BEGIN
        UPDATE [Motion]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetProtocolRowVersionINSERT]
    AFTER INSERT ON [Protocol]
    BEGIN
        UPDATE [Protocol]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetProtocolRowVersionUPDATE]
    AFTER UPDATE ON [Protocol]
    BEGIN
        UPDATE [Protocol]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetProtocolXExerciseTypeRowVersionINSERT]
    AFTER INSERT ON [ProtocolXExerciseType]
    BEGIN
        UPDATE [ProtocolXExerciseType]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetProtocolXExerciseTypeRowVersionUPDATE]
    AFTER UPDATE ON [ProtocolXExerciseType]
    BEGIN
        UPDATE [ProtocolXExerciseType]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetSessionRowVersionINSERT]
    AFTER INSERT ON [Session]
    BEGIN
        UPDATE [Session]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetSessionRowVersionUPDATE]
    AFTER UPDATE ON [Session]
    BEGIN
        UPDATE [Session]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetSetDataRowVersionINSERT]
    AFTER INSERT ON [SetData]
    BEGIN
        UPDATE [SetData]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetSetDataRowVersionUPDATE]
    AFTER UPDATE ON [SetData]
    BEGIN
        UPDATE [SetData]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetSetRowVersionINSERT]
    AFTER INSERT ON [Set]
    BEGIN
        UPDATE [Set]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;

CREATE TRIGGER [SetSetRowVersionUPDATE]
    AFTER UPDATE ON [Set]
    BEGIN
        UPDATE [Set]
        SET LocalDbRowVersion = LocalDbRowVersion + 1
        WHERE rowid = NEW.rowid;
    END;
