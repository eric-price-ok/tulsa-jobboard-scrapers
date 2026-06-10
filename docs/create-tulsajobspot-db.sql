-- =============================================================================
-- TulsaJobSpot Database Schema
-- Based on original schema with the following changes:
--   - Added: users, user_company_roles, company_invites, saved_jobs,
--            saved_searches, notifications, applications
--   - Added: application_method to joblistings
--   - Fixed: approved defaults to FALSE on company and joblistings
--   - Fixed: cities — removed freetext city from companysite, FK only
--   - Fixed: skills consolidation — dropped jobskills (AI freetext),
--            all skills go through skills + joblistingskills
--   - Fixed: notes.created_by is now a FK to users
--   - Added: is_served flag to cities (admin-controlled)
--   - Added: site_level roles (is_admin, is_moderator) on users
-- =============================================================================


-- =============================================================================
-- FUNCTIONS (defined first, referenced by triggers)
-- =============================================================================

CREATE OR REPLACE FUNCTION public.update_updated_at_column()
    RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.update_scraping_duration()
    RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.completed_at IS NOT NULL AND OLD.completed_at IS NULL THEN
        NEW.duration_seconds = EXTRACT(EPOCH FROM (NEW.completed_at - NEW.started_at));
    END IF;
    RETURN NEW;
END;
$$;


-- =============================================================================
-- LOOKUP / REFERENCE TABLES
-- =============================================================================

CREATE TABLE public.country (
    id          serial4 NOT NULL,
    name        varchar(100) NOT NULL,
    iso_code_2  bpchar(2) NULL,
    iso_code_3  bpchar(3) NULL,
    is_active   bool DEFAULT true NOT NULL,
    created_at  timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT country_name_key UNIQUE (name),
    CONSTRAINT country_pkey PRIMARY KEY (id)
);

CREATE TABLE public.state (
    id          serial4 NOT NULL,
    country_id  int4 NOT NULL,
    name        varchar(100) NOT NULL,
    abbreviation varchar(10) NULL,
    is_active   bool DEFAULT true NOT NULL,
    created_at  timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT state_pkey PRIMARY KEY (id),
    CONSTRAINT unique_state_per_country UNIQUE (country_id, name),
    CONSTRAINT state_countryid_fkey FOREIGN KEY (country_id)
        REFERENCES public.country(id) ON DELETE CASCADE
);
CREATE INDEX idx_state_country ON public.state USING btree (country_id);

-- Admin controls which cities are served by this instance of the site.
-- is_served=true means the city appears in dropdowns and filters.
CREATE TABLE public.cities (
    id          serial4 NOT NULL,
    city_name   varchar(100) NOT NULL,
    state_id    int4 NULL,
    is_served   bool DEFAULT false NOT NULL,
    sort_order  int4 NULL,
    CONSTRAINT cities_pkey PRIMARY KEY (id),
    CONSTRAINT fk_cities_state FOREIGN KEY (state_id)
        REFERENCES public.state(id) ON DELETE SET NULL
);
CREATE INDEX idx_cities_is_served ON public.cities USING btree (is_served);
CREATE INDEX idx_cities_state ON public.cities USING btree (state_id);

CREATE TABLE public.company_type (
    id          serial4 NOT NULL,
    name        varchar(50) NOT NULL,
    description text NULL,
    is_active   bool DEFAULT true NOT NULL,
    created_at  timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT company_type_name_key UNIQUE (name),
    CONSTRAINT company_type_pkey PRIMARY KEY (id)
);

CREATE TABLE public.companysitetype (
    id          serial4 NOT NULL,
    name        varchar(50) NOT NULL,
    description text NULL,
    is_active   bool DEFAULT true NOT NULL,
    created_at  timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT companysitetype_name_key UNIQUE (name),
    CONSTRAINT companysitetype_pkey PRIMARY KEY (id)
);

CREATE TABLE public.benefits (
    id          serial4 NOT NULL,
    name        varchar(100) NOT NULL,
    description text NULL,
    category    varchar(50) NULL,
    is_active   bool DEFAULT true NOT NULL,
    created_at  timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT benefits_name_key UNIQUE (name),
    CONSTRAINT benefits_pkey PRIMARY KEY (id)
);

CREATE TABLE public.features (
    id          serial4 NOT NULL,
    name        varchar(100) NOT NULL,
    description text NULL,
    category    varchar(50) NULL,
    is_active   bool DEFAULT true NOT NULL,
    created_at  timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT features_name_key UNIQUE (name),
    CONSTRAINT features_pkey PRIMARY KEY (id)
);

CREATE TABLE public.functions (
    id          serial4 NOT NULL,
    name        varchar(100) NOT NULL,
    description text NULL,
    is_active   bool DEFAULT true NOT NULL,
    created_at  timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT functions_name_key UNIQUE (name),
    CONSTRAINT functions_pkey PRIMARY KEY (id)
);

CREATE TABLE public.functionspecialties (
    id          serial4 NOT NULL,
    function_id int4 NOT NULL,
    specialty   varchar(100) NOT NULL,
    description text NULL,
    is_active   bool DEFAULT true NOT NULL,
    created_at  timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT functionspecialties_pkey PRIMARY KEY (id),
    CONSTRAINT unique_function_specialty UNIQUE (function_id, specialty),
    CONSTRAINT functionspecialties_functionid_fkey FOREIGN KEY (function_id)
        REFERENCES public.functions(id) ON DELETE CASCADE
);
CREATE INDEX idx_functionspecialties_function ON public.functionspecialties USING btree (function_id);

CREATE TABLE public.industries (
    id          serial4 NOT NULL,
    name        varchar(100) NOT NULL,
    description text NULL,
    is_active   bool DEFAULT true NOT NULL,
    sort_order  int4 NULL,
    created_at  timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT industries_name_key UNIQUE (name),
    CONSTRAINT industries_pkey PRIMARY KEY (id)
);

CREATE TABLE public.experience (
    id        serial4 NOT NULL,
    name      varchar NOT NULL,
    is_active bool DEFAULT true NOT NULL,
    CONSTRAINT experience_pk PRIMARY KEY (id)
);

CREATE TABLE public.jobstatus (
    id          serial4 NOT NULL,
    name        varchar(50) NOT NULL,
    description text NULL,
    is_active   bool DEFAULT true NOT NULL,
    created_at  timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT jobstatus_name_key UNIQUE (name),
    CONSTRAINT jobstatus_pkey PRIMARY KEY (id)
);

CREATE TABLE public.jobtype (
    id         serial4 NOT NULL,
    name       varchar(50) NOT NULL,
    created_at timestamp DEFAULT CURRENT_TIMESTAMP NULL,
    is_active  bool DEFAULT true NOT NULL,
    CONSTRAINT jobtype_name_key UNIQUE (name),
    CONSTRAINT jobtype_pkey PRIMARY KEY (id)
);

CREATE TABLE public.officelocations (
    id         serial4 NOT NULL,
    name       varchar(50) NOT NULL,
    created_at timestamp DEFAULT CURRENT_TIMESTAMP NULL,
    is_active  bool DEFAULT true NOT NULL,
    CONSTRAINT officelocations_name_key UNIQUE (name),
    CONSTRAINT officelocations_pkey PRIMARY KEY (id)
);

CREATE TABLE public.social_media_types (
    id         serial4 NOT NULL,
    name       varchar(50) NOT NULL,
    base_url   varchar(255) NOT NULL,
    created_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT social_media_types_name_key UNIQUE (name),
    CONSTRAINT social_media_types_pkey PRIMARY KEY (id)
);
CREATE INDEX idx_social_media_types_name ON public.social_media_types USING btree (name);

CREATE TABLE public.certification_providers (
    id              serial4 NOT NULL,
    name            varchar(100) NOT NULL,
    website         varchar(255) NULL,
    industry_focus  varchar(100) NULL,
    CONSTRAINT certification_providers_pkey PRIMARY KEY (id)
);

CREATE TABLE public.certifications (
    id                      serial4 NOT NULL,
    name                    varchar(200) NOT NULL,
    code                    varchar(50) NULL,
    provider_id             int4 NULL,
    category                varchar(100) NULL,
    level                   varchar(50) NULL,
    typical_duration_months int4 NULL,
    description             text NULL,
    is_active               bool DEFAULT true NULL,
    CONSTRAINT certifications_pkey PRIMARY KEY (id),
    CONSTRAINT certifications_provider_id_fkey FOREIGN KEY (provider_id)
        REFERENCES public.certification_providers(id)
);

-- Consolidated skills table. jobskills (AI freetext) is removed.
-- All skills — whether entered manually or extracted by AI — are
-- normalized into this table first, then linked via joblistingskills.
CREATE TABLE public.skill_categories (
    id         serial4 NOT NULL,
    name       varchar(100) NOT NULL,
    active     bool DEFAULT true NOT NULL,
    sort_order int4 DEFAULT 0 NOT NULL,
    created_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT skill_categories_name_key UNIQUE (name),
    CONSTRAINT skill_categories_pkey PRIMARY KEY (id)
);
CREATE INDEX idx_skill_categories_active     ON public.skill_categories USING btree (active);
CREATE INDEX idx_skill_categories_sort_order ON public.skill_categories USING btree (sort_order);

CREATE TRIGGER update_skill_categories_updated_at
    BEFORE UPDATE ON public.skill_categories
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TABLE public.skills (
    id                  serial4 NOT NULL,
    name                varchar(100) NOT NULL,
    description         text NULL,
    is_active           bool DEFAULT true NOT NULL,
    skill_category_id   int4 NULL,
    certification_id    int4 NULL,
    created_at          timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT skills_name_key UNIQUE (name),
    CONSTRAINT skills_pkey PRIMARY KEY (id),
    CONSTRAINT fk_skills_skill_category FOREIGN KEY (skill_category_id)
        REFERENCES public.skill_categories(id) ON DELETE SET NULL,
    CONSTRAINT skills_certification_id_fkey FOREIGN KEY (certification_id)
        REFERENCES public.certifications(id)
);
CREATE INDEX idx_skills_category ON public.skills USING btree (skill_category_id);


-- =============================================================================
-- USERS
-- Site-level roles (is_admin, is_moderator) live here.
-- Company-scoped roles live in user_company_roles.
-- OAuth only — no password storage.
-- =============================================================================

CREATE TABLE public.users (
    id                  serial4 NOT NULL,
    email               varchar(255) NOT NULL,
    full_name           varchar(255) NULL,
    avatar_url          varchar(500) NULL,
    oauth_provider      varchar(50) NOT NULL,   -- 'google', 'linkedin', etc.
    oauth_subject       varchar(255) NOT NULL,  -- provider's unique user ID
    is_admin            bool DEFAULT false NOT NULL,
    is_moderator        bool DEFAULT false NOT NULL,
    is_active           bool DEFAULT true NOT NULL,
    created_at          timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at          timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    last_login_at       timestamp NULL,
    CONSTRAINT users_pkey PRIMARY KEY (id),
    CONSTRAINT users_email_key UNIQUE (email),
    CONSTRAINT unique_oauth_identity UNIQUE (oauth_provider, oauth_subject)
);
CREATE INDEX idx_users_email        ON public.users USING btree (email);
CREATE INDEX idx_users_is_admin     ON public.users USING btree (is_admin);
CREATE INDEX idx_users_is_moderator ON public.users USING btree (is_moderator);

CREATE TRIGGER update_users_updated_at
    BEFORE UPDATE ON public.users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();


-- =============================================================================
-- COMPANY
-- =============================================================================

CREATE TABLE public.company (
    id                      serial4 NOT NULL,
    common_name             varchar(255) NOT NULL,
    legal_name              varchar(255) NULL,
    website                 varchar(500) NULL,
    jobboard                varchar(500) NULL,
    date_founded            date NULL,
    date_closed             date NULL,
    defunct                 bool DEFAULT false NOT NULL,
    approved                bool DEFAULT false NOT NULL,  -- fixed: was DEFAULT true
    approved_by             int4 NULL,                   -- FK to users (admin/moderator)
    approved_at             timestamp NULL,
    company_type            int4 NOT NULL,
    description             text NULL,
    company_size            varchar(50) NULL,
    is_scraped              bool DEFAULT false NOT NULL,  -- true = sourced by scraper
    created_at              timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at              timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    last_full_scrape_completed timestamp NULL,
    CONSTRAINT company_name_not_empty CHECK (length(TRIM(BOTH FROM common_name)) > 0),
    CONSTRAINT company_pkey PRIMARY KEY (id),
    CONSTRAINT valid_company_dates CHECK (
        (date_closed IS NULL) OR (date_founded IS NULL) OR (date_closed > date_founded)
    ),
    CONSTRAINT company_companytype_fkey FOREIGN KEY (company_type)
        REFERENCES public.company_type(id) ON DELETE SET NULL,
    CONSTRAINT company_approved_by_fkey FOREIGN KEY (approved_by)
        REFERENCES public.users(id) ON DELETE SET NULL
);
CREATE INDEX idx_company_approved    ON public.company USING btree (approved);
CREATE INDEX idx_company_common_name ON public.company USING btree (common_name);
CREATE INDEX idx_company_type        ON public.company USING btree (company_type);
CREATE INDEX idx_company_is_scraped  ON public.company USING btree (is_scraped);

CREATE TRIGGER update_company_updated_at
    BEFORE UPDATE ON public.company
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();


-- =============================================================================
-- USER <-> COMPANY ROLES
-- Replaces the old single-owner model.
-- role: 'company_admin' | 'job_poster'
-- One company_admin per company enforced at application layer.
-- A user may hold roles at multiple companies (entrepreneur / fractional HR).
-- =============================================================================

CREATE TABLE public.user_company_roles (
    id              serial4 NOT NULL,
    user_id         int4 NOT NULL,
    company_id      int4 NOT NULL,
    role            varchar(20) NOT NULL,
    approved        bool DEFAULT false NOT NULL,
    approved_by     int4 NULL,   -- user_id of admin/moderator or company_admin
    approved_at     timestamp NULL,
    created_at      timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at      timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT user_company_roles_pkey PRIMARY KEY (id),
    CONSTRAINT unique_user_company_role UNIQUE (user_id, company_id),
    CONSTRAINT valid_company_role CHECK (role IN ('company_admin', 'job_poster')),
    CONSTRAINT ucr_user_fkey FOREIGN KEY (user_id)
        REFERENCES public.users(id) ON DELETE CASCADE,
    CONSTRAINT ucr_company_fkey FOREIGN KEY (company_id)
        REFERENCES public.company(id) ON DELETE CASCADE,
    CONSTRAINT ucr_approved_by_fkey FOREIGN KEY (approved_by)
        REFERENCES public.users(id) ON DELETE SET NULL
);
CREATE INDEX idx_ucr_user    ON public.user_company_roles USING btree (user_id);
CREATE INDEX idx_ucr_company ON public.user_company_roles USING btree (company_id);
CREATE INDEX idx_ucr_role    ON public.user_company_roles USING btree (role);
CREATE INDEX idx_ucr_pending ON public.user_company_roles USING btree (company_id, approved)
    WHERE approved = false;

CREATE TRIGGER update_user_company_roles_updated_at
    BEFORE UPDATE ON public.user_company_roles
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();


-- =============================================================================
-- COMPANY INVITES
-- Token-based. company_admin invites a job_poster by email.
-- If the email matches an existing user they are auto-linked on accept.
-- =============================================================================

CREATE TABLE public.company_invites (
    id              serial4 NOT NULL,
    company_id      int4 NOT NULL,
    invited_by      int4 NOT NULL,   -- user_id of company_admin or admin/moderator
    invited_email   varchar(255) NOT NULL,
    role            varchar(20) NOT NULL DEFAULT 'job_poster',
    token           varchar(128) NOT NULL,
    accepted        bool DEFAULT false NOT NULL,
    accepted_at     timestamp NULL,
    accepted_by     int4 NULL,       -- user_id who accepted
    expires_at      timestamp NOT NULL,
    created_at      timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT company_invites_pkey PRIMARY KEY (id),
    CONSTRAINT company_invites_token_key UNIQUE (token),
    CONSTRAINT valid_invite_role CHECK (role IN ('company_admin', 'job_poster')),
    CONSTRAINT company_invites_company_fkey FOREIGN KEY (company_id)
        REFERENCES public.company(id) ON DELETE CASCADE,
    CONSTRAINT company_invites_invited_by_fkey FOREIGN KEY (invited_by)
        REFERENCES public.users(id) ON DELETE CASCADE,
    CONSTRAINT company_invites_accepted_by_fkey FOREIGN KEY (accepted_by)
        REFERENCES public.users(id) ON DELETE SET NULL
);
CREATE INDEX idx_company_invites_company ON public.company_invites USING btree (company_id);
CREATE INDEX idx_company_invites_token   ON public.company_invites USING btree (token);
CREATE INDEX idx_company_invites_email   ON public.company_invites USING btree (invited_email);


-- =============================================================================
-- COMPANY SUPPORTING TABLES
-- =============================================================================

CREATE TABLE public.companysite (
    id              serial4 NOT NULL,
    company_id      int4 NOT NULL,
    site_type       int4 NULL,
    address1        varchar(255) NULL,
    address2        varchar(255) NULL,
    country_id      int4 NULL,
    state_id        int4 NULL,
    city_id         int4 NULL,          -- FK only; freetext city column removed
    postcode        varchar(10) NULL,
    phone           varchar(50) NULL,
    site_web        varchar(500) NULL,
    site_job_board  varchar(500) NULL,
    shortname       varchar NULL,
    is_headquarters bool DEFAULT false NOT NULL,
    is_active       bool DEFAULT true NOT NULL,
    employee_count  int4 NULL,
    created_at      timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at      timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT companysite_pkey PRIMARY KEY (id),
    CONSTRAINT valid_employee_count CHECK ((employee_count IS NULL) OR (employee_count >= 0)),
    CONSTRAINT companysite_companyid_fkey FOREIGN KEY (company_id)
        REFERENCES public.company(id) ON DELETE CASCADE,
    CONSTRAINT companysite_countryid_fkey FOREIGN KEY (country_id)
        REFERENCES public.country(id) ON DELETE SET NULL,
    CONSTRAINT companysite_stateid_fkey FOREIGN KEY (state_id)
        REFERENCES public.state(id) ON DELETE SET NULL,
    CONSTRAINT companysite_cityid_fkey FOREIGN KEY (city_id)
        REFERENCES public.cities(id) ON DELETE SET NULL,
    CONSTRAINT companysite_sitetype_fkey FOREIGN KEY (site_type)
        REFERENCES public.companysitetype(id) ON DELETE SET NULL
);
CREATE INDEX idx_companysite_company  ON public.companysite USING btree (company_id);
CREATE INDEX idx_companysite_country  ON public.companysite USING btree (country_id);
CREATE INDEX idx_companysite_state    ON public.companysite USING btree (state_id);
CREATE INDEX idx_companysite_city     ON public.companysite USING btree (city_id);
CREATE INDEX idx_companysite_type     ON public.companysite USING btree (site_type);

CREATE TRIGGER update_companysite_updated_at
    BEFORE UPDATE ON public.companysite
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TABLE public.company_socials (
    id                  serial4 NOT NULL,
    company_id          int4 NOT NULL,
    social_media_type_id int4 NOT NULL,
    company_url         varchar(500) NOT NULL,
    is_active           bool DEFAULT true NOT NULL,
    created_at          timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at          timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT company_socials_pkey PRIMARY KEY (id),
    CONSTRAINT unique_company_social_media UNIQUE (company_id, social_media_type_id),
    CONSTRAINT valid_company_url CHECK ((company_url)::text ~* '^https?://[^\s]+$'),
    CONSTRAINT fk_company_socials_company FOREIGN KEY (company_id)
        REFERENCES public.company(id) ON DELETE CASCADE,
    CONSTRAINT fk_company_socials_social_media_type FOREIGN KEY (social_media_type_id)
        REFERENCES public.social_media_types(id) ON DELETE CASCADE
);
CREATE INDEX idx_company_socials_company ON public.company_socials USING btree (company_id);
CREATE INDEX idx_company_socials_type    ON public.company_socials USING btree (social_media_type_id);
CREATE INDEX idx_company_socials_active  ON public.company_socials USING btree (is_active);

CREATE TRIGGER update_company_socials_updated_at
    BEFORE UPDATE ON public.company_socials
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TABLE public.companybenefits (
    id           serial4 NOT NULL,
    company_id   int4 NOT NULL,
    benefit_id   int4 NOT NULL,
    is_featured  bool DEFAULT false NOT NULL,
    created_at   timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    last_updated timestamp NULL,
    CONSTRAINT companybenefits_pkey PRIMARY KEY (id),
    CONSTRAINT unique_company_benefit UNIQUE (company_id, benefit_id),
    CONSTRAINT companybenefits_benefitid_fkey FOREIGN KEY (benefit_id)
        REFERENCES public.benefits(id) ON DELETE CASCADE,
    CONSTRAINT companybenefits_companyid_fkey FOREIGN KEY (company_id)
        REFERENCES public.company(id) ON DELETE CASCADE
);

CREATE TABLE public.companyfunctions (
    id          serial4 NOT NULL,
    company_id  int4 NOT NULL,
    function_id int4 NOT NULL,
    created_at  timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT companyfunctions_pkey PRIMARY KEY (id),
    CONSTRAINT unique_company_function UNIQUE (company_id, function_id),
    CONSTRAINT companyfunctions_companyid_fkey FOREIGN KEY (company_id)
        REFERENCES public.company(id) ON DELETE CASCADE,
    CONSTRAINT companyfunctions_functionid_fkey FOREIGN KEY (function_id)
        REFERENCES public.functions(id) ON DELETE CASCADE
);
CREATE INDEX idx_companyfunctions_company  ON public.companyfunctions USING btree (company_id);
CREATE INDEX idx_companyfunctions_function ON public.companyfunctions USING btree (function_id);

CREATE TABLE public.companyindustries (
    id          serial4 NOT NULL,
    company_id  int4 NOT NULL,
    industry_id int4 NOT NULL,
    created_at  timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT companyindustries_pkey PRIMARY KEY (id),
    CONSTRAINT unique_company_industry UNIQUE (company_id, industry_id),
    CONSTRAINT companyindustries_companyid_fkey FOREIGN KEY (company_id)
        REFERENCES public.company(id) ON DELETE CASCADE,
    CONSTRAINT companyindustries_industryid_fkey FOREIGN KEY (industry_id)
        REFERENCES public.industries(id) ON DELETE CASCADE
);
CREATE INDEX idx_companyindustries_company  ON public.companyindustries USING btree (company_id);
CREATE INDEX idx_companyindustries_industry ON public.companyindustries USING btree (industry_id);

CREATE TABLE public.companytechnologies (
    id               serial4 NOT NULL,
    company_id       int4 NOT NULL,
    skill_id         int4 NOT NULL,
    proficiency_level varchar(20) DEFAULT 'unknown' NULL,
    years_experience int4 NULL,
    is_primary       bool DEFAULT false NOT NULL,
    created_at       timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT companytechnologies_pkey PRIMARY KEY (id),
    CONSTRAINT unique_company_skill UNIQUE (company_id, skill_id),
    CONSTRAINT valid_proficiency CHECK (
        proficiency_level IN ('beginner','intermediate','advanced','expert','unknown')
    ),
    CONSTRAINT valid_years CHECK ((years_experience IS NULL) OR (years_experience >= 0)),
    CONSTRAINT companytechnologies_companyid_fkey FOREIGN KEY (company_id)
        REFERENCES public.company(id) ON DELETE CASCADE,
    CONSTRAINT companytechnologies_skillid_fkey FOREIGN KEY (skill_id)
        REFERENCES public.skills(id) ON DELETE CASCADE
);
CREATE INDEX idx_companytechnologies_company ON public.companytechnologies USING btree (company_id);
CREATE INDEX idx_companytechnologies_skill   ON public.companytechnologies USING btree (skill_id);

-- Admin/moderator notes on companies.
-- created_by is now a proper FK to users.
CREATE TABLE public.notes (
    id          serial4 NOT NULL,
    company_id  int4 NOT NULL,
    shortname   varchar(100) NULL,
    fullnote    text NULL,
    date        date NOT NULL,
    created_by  int4 NULL,          -- FK to users; was varchar(100)
    note_type   varchar(50) DEFAULT 'general' NULL,
    is_private  bool DEFAULT false NOT NULL,
    created_at  timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at  timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT notes_pkey PRIMARY KEY (id),
    CONSTRAINT notes_companyid_fkey FOREIGN KEY (company_id)
        REFERENCES public.company(id) ON DELETE CASCADE,
    CONSTRAINT notes_createdby_fkey FOREIGN KEY (created_by)
        REFERENCES public.users(id) ON DELETE SET NULL
);

CREATE TRIGGER update_notes_updated_at
    BEFORE UPDATE ON public.notes
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();


-- =============================================================================
-- JOB LISTINGS
-- application_method drives UX:
--   'external_url' — link out (scraped jobs, companies with own board)
--   'email'        — small employers, apply via email
--   'in_platform'  — full application captured here
-- approved defaults to FALSE.
-- posted_by links to the user who submitted (NULL for scraper-sourced jobs).
-- =============================================================================

CREATE TABLE public.joblistings (
    id                      serial4 NOT NULL,
    company_id              int4 NOT NULL,
    company_site_id         int4 NULL,
    posted_by               int4 NULL,          -- FK to users; NULL = scraper
    job_title               varchar(500) NOT NULL,
    job_description         text NULL,
    posting_id              varchar(255) NULL,
    posting_url             varchar(1000) NULL,
    application_method      varchar(20) NOT NULL DEFAULT 'external_url',
    application_email       varchar(255) NULL,   -- used when method = 'email'
    date_posted             date NULL,
    date_closed             date NULL,
    perpetual               bool DEFAULT false NOT NULL,
    approved                bool DEFAULT false NOT NULL,  -- fixed: was DEFAULT true
    approved_by             int4 NULL,
    approved_at             timestamp NULL,
    job_status_id           int4 NOT NULL,
    function                int4 NULL,
    specialty               int4 NULL,
    job_type_id             int4 NULL,
    experience_id           int4 NULL,
    office_location_id      int4 NULL,
    city_id                 int4 NULL,
    minimum_salary          numeric(10, 2) NULL,
    maximum_salary          numeric(10, 2) NULL,
    pay_frequency           varchar(50) NULL,
    experience_years_min    int4 NULL,
    experience_years_max    int4 NULL,
    associate_degree        varchar(20) DEFAULT 'not_mentioned' NULL,
    bachelors_degree        varchar(20) DEFAULT 'not_mentioned' NULL,
    masters_degree          varchar(20) DEFAULT 'not_mentioned' NULL,
    doctorate_degree        varchar(20) DEFAULT 'not_mentioned' NULL,
    first_shift             bool DEFAULT false NULL,
    second_shift            bool DEFAULT false NULL,
    third_shift             bool DEFAULT false NULL,
    rotating_shift          bool DEFAULT false NULL,
    flexible_schedule       bool DEFAULT false NULL,
    weekends_required       varchar(20) DEFAULT 'not_mentioned' NULL,
    evenings_required       varchar(20) DEFAULT 'not_mentioned' NULL,
    holidays_required       varchar(20) DEFAULT 'not_mentioned' NULL,
    travel_requirements     varchar(20) DEFAULT 'not_mentioned' NULL,
    travel_percentage       int4 NULL,
    is_temporary            bool DEFAULT false NULL,
    is_seasonal             bool DEFAULT false NULL,
    is_volunteer            bool DEFAULT false NULL,
    is_individual_contributor bool DEFAULT false NULL,
    is_people_manager       bool DEFAULT false NULL,
    -- scraping metadata
    source_job_board        varchar(100) NULL,
    external_job_id         varchar(255) NULL,
    scraping_hash           varchar(64) NULL,
    last_scraped            timestamp DEFAULT CURRENT_TIMESTAMP NULL,
    extraction_confidence   numeric(3, 2) DEFAULT 1.0 NULL,
    extraction_model        varchar(50) DEFAULT 'claude-sonnet' NULL,
    extraction_timestamp    timestamptz NULL,
    extraction_version      varchar(10) DEFAULT '2.0' NULL,
    raw_text_length         int4 NULL,
    created_at              timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at              timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,

    CONSTRAINT joblistings_pkey PRIMARY KEY (id),
    CONSTRAINT job_title_not_empty CHECK (length(TRIM(BOTH FROM job_title)) > 0),
    CONSTRAINT valid_application_method CHECK (
        application_method IN ('external_url', 'email', 'in_platform')
    ),
    CONSTRAINT valid_application_email CHECK (
        application_method != 'email' OR application_email IS NOT NULL
    ),
    CONSTRAINT valid_posting_url CHECK (
        (posting_url IS NULL) OR ((posting_url)::text ~* '^https?://')
    ),
    CONSTRAINT positive_salary_values CHECK (
        (minimum_salary IS NULL) OR (minimum_salary > 0)
    ),
    CONSTRAINT valid_salary_range CHECK (
        (minimum_salary IS NULL) OR (maximum_salary IS NULL) OR (minimum_salary <= maximum_salary)
    ),
    CONSTRAINT valid_pay_frequency CHECK (
        pay_frequency IS NULL OR pay_frequency IN
        ('hourly','daily','weekly','biweekly','monthly','annually')
    ),
    CONSTRAINT valid_job_dates CHECK (
        (date_closed IS NULL) OR (date_posted IS NULL) OR (date_closed >= date_posted)
    ),
    CONSTRAINT valid_experience_years CHECK (
        ((experience_years_min IS NULL) OR (experience_years_min >= 0)) AND
        ((experience_years_max IS NULL) OR (experience_years_max >= 0)) AND
        ((experience_years_min IS NULL) OR (experience_years_max IS NULL) OR
         (experience_years_min <= experience_years_max))
    ),
    CONSTRAINT valid_degree_requirements CHECK (
        associate_degree IN ('required','preferred','not_mentioned') AND
        bachelors_degree IN ('required','preferred','not_mentioned') AND
        masters_degree   IN ('required','preferred','not_mentioned') AND
        doctorate_degree IN ('required','preferred','not_mentioned')
    ),
    CONSTRAINT valid_availability_requirements CHECK (
        weekends_required IN ('required','occasional','not_mentioned') AND
        evenings_required IN ('required','occasional','not_mentioned') AND
        holidays_required IN ('required','occasional','not_mentioned')
    ),
    CONSTRAINT valid_travel_requirements CHECK (
        travel_requirements IN ('none','minimal','moderate','extensive','constant','not_mentioned')
    ),
    CONSTRAINT valid_travel_percentage CHECK (
        (travel_percentage IS NULL) OR
        (travel_percentage >= 0 AND travel_percentage <= 100)
    ),
    CONSTRAINT valid_extraction_confidence CHECK (
        (extraction_confidence IS NULL) OR
        (extraction_confidence >= 0.0 AND extraction_confidence <= 1.0)
    ),
    CONSTRAINT joblistings_companyid_fkey FOREIGN KEY (company_id)
        REFERENCES public.company(id) ON DELETE CASCADE,
    CONSTRAINT joblistings_companysiteid_fkey FOREIGN KEY (company_site_id)
        REFERENCES public.companysite(id) ON DELETE SET NULL,
    CONSTRAINT joblistings_posted_by_fkey FOREIGN KEY (posted_by)
        REFERENCES public.users(id) ON DELETE SET NULL,
    CONSTRAINT joblistings_approved_by_fkey FOREIGN KEY (approved_by)
        REFERENCES public.users(id) ON DELETE SET NULL,
    CONSTRAINT joblistings_jobstatusid_fkey FOREIGN KEY (job_status_id)
        REFERENCES public.jobstatus(id),
    CONSTRAINT joblistings_function_fkey FOREIGN KEY (function)
        REFERENCES public.functions(id) ON DELETE SET NULL,
    CONSTRAINT joblistings_specialty_fkey FOREIGN KEY (specialty)
        REFERENCES public.functionspecialties(id) ON DELETE SET NULL,
    CONSTRAINT fk_joblistings_jobtype FOREIGN KEY (job_type_id)
        REFERENCES public.jobtype(id),
    CONSTRAINT fk_joblistings_experience FOREIGN KEY (experience_id)
        REFERENCES public.experience(id),
    CONSTRAINT fk_joblistings_officelocation FOREIGN KEY (office_location_id)
        REFERENCES public.officelocations(id),
    CONSTRAINT fk_joblistings_city FOREIGN KEY (city_id)
        REFERENCES public.cities(id)
);
CREATE INDEX idx_joblistings_company        ON public.joblistings USING btree (company_id);
CREATE INDEX idx_joblistings_approved       ON public.joblistings USING btree (approved);
CREATE INDEX idx_joblistings_posted_by      ON public.joblistings USING btree (posted_by);
CREATE INDEX idx_joblistings_status         ON public.joblistings USING btree (job_status_id);
CREATE INDEX idx_joblistings_company_status ON public.joblistings USING btree (company_id, job_status_id);
CREATE INDEX idx_joblistings_date_status    ON public.joblistings USING btree (date_posted, job_status_id);
CREATE INDEX idx_joblistings_posted_date    ON public.joblistings USING btree (date_posted DESC);
CREATE INDEX idx_joblistings_function       ON public.joblistings USING btree (function);
CREATE INDEX idx_joblistings_function_status ON public.joblistings USING btree (function, job_status_id);
CREATE INDEX idx_joblistings_specialty      ON public.joblistings USING btree (specialty);
CREATE INDEX idx_joblistings_experience     ON public.joblistings USING btree (experience_id);
CREATE INDEX idx_joblistings_salary_range   ON public.joblistings USING btree (minimum_salary, maximum_salary)
    WHERE minimum_salary IS NOT NULL;
CREATE INDEX idx_joblistings_hash           ON public.joblistings USING btree (scraping_hash);
CREATE INDEX idx_joblistings_last_scraped   ON public.joblistings USING btree (last_scraped);
CREATE INDEX idx_joblistings_url            ON public.joblistings USING btree (posting_url);
CREATE INDEX idx_joblistings_site           ON public.joblistings USING btree (company_site_id);
CREATE INDEX idx_joblistings_application    ON public.joblistings USING btree (application_method);
CREATE INDEX idx_joblistings_city           ON public.joblistings USING btree (city_id);
-- Partial index for pending approval queue
CREATE INDEX idx_joblistings_pending        ON public.joblistings USING btree (created_at)
    WHERE approved = false;

CREATE TRIGGER update_joblistings_updated_at
    BEFORE UPDATE ON public.joblistings
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Consolidated skills for job listings.
-- jobskills (AI freetext) table is removed; AI-extracted skills are
-- normalized into skills first, then linked here.
CREATE TABLE public.joblistingskills (
    id                  serial4 NOT NULL,
    job_listing_id      int4 NOT NULL,
    skill_id            int4 NOT NULL,
    required_skill      bool DEFAULT false NOT NULL,
    preferred_skill     bool DEFAULT false NOT NULL,
    years_required      int4 NULL,
    proficiency_level   varchar(20) DEFAULT 'unknown' NULL,
    extraction_method   varchar(20) DEFAULT 'manual' NULL,  -- 'manual','ai','hybrid'
    confidence_score    numeric(3, 2) NULL,
    created_at          timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT joblistingskills_pkey PRIMARY KEY (id),
    CONSTRAINT unique_job_skill UNIQUE (job_listing_id, skill_id),
    CONSTRAINT valid_job_proficiency CHECK (
        proficiency_level IN ('beginner','intermediate','advanced','expert','unknown')
    ),
    CONSTRAINT valid_job_years CHECK ((years_required IS NULL) OR (years_required >= 0)),
    CONSTRAINT valid_extraction_method CHECK (
        extraction_method IN ('manual','ai','hybrid')
    ),
    CONSTRAINT valid_skill_confidence CHECK (
        (confidence_score IS NULL) OR
        (confidence_score >= 0 AND confidence_score <= 1)
    ),
    CONSTRAINT joblistingskills_joblistingid_fkey FOREIGN KEY (job_listing_id)
        REFERENCES public.joblistings(id) ON DELETE CASCADE,
    CONSTRAINT joblistingskills_skillid_fkey FOREIGN KEY (skill_id)
        REFERENCES public.skills(id) ON DELETE CASCADE
);
CREATE INDEX idx_joblistingskills_job   ON public.joblistingskills USING btree (job_listing_id);
CREATE INDEX idx_joblistingskills_skill ON public.joblistingskills USING btree (skill_id);


-- =============================================================================
-- IN-PLATFORM APPLICATIONS
-- Only used when joblistings.application_method = 'in_platform'.
-- =============================================================================

CREATE TABLE public.applications (
    id                  serial4 NOT NULL,
    job_listing_id      int4 NOT NULL,
    applicant_user_id   int4 NULL,       -- NULL if applied without account
    applicant_name      varchar(255) NOT NULL,
    applicant_email     varchar(255) NOT NULL,
    applicant_phone     varchar(50) NULL,
    cover_letter        text NULL,
    resume_filename     varchar(500) NULL,  -- stored filename/path
    resume_uploaded_at  timestamp NULL,
    status              varchar(30) DEFAULT 'submitted' NOT NULL,
    status_updated_at   timestamp NULL,
    status_updated_by   int4 NULL,       -- user_id (company_admin or admin)
    notes               text NULL,
    created_at          timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at          timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT applications_pkey PRIMARY KEY (id),
    CONSTRAINT valid_application_status CHECK (
        status IN ('submitted','reviewing','shortlisted','rejected','hired','withdrawn')
    ),
    CONSTRAINT applications_job_fkey FOREIGN KEY (job_listing_id)
        REFERENCES public.joblistings(id) ON DELETE CASCADE,
    CONSTRAINT applications_user_fkey FOREIGN KEY (applicant_user_id)
        REFERENCES public.users(id) ON DELETE SET NULL,
    CONSTRAINT applications_status_by_fkey FOREIGN KEY (status_updated_by)
        REFERENCES public.users(id) ON DELETE SET NULL
);
CREATE INDEX idx_applications_job         ON public.applications USING btree (job_listing_id);
CREATE INDEX idx_applications_user        ON public.applications USING btree (applicant_user_id);
CREATE INDEX idx_applications_status      ON public.applications USING btree (status);
CREATE INDEX idx_applications_email       ON public.applications USING btree (applicant_email);

CREATE TRIGGER update_applications_updated_at
    BEFORE UPDATE ON public.applications
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();


-- =============================================================================
-- USER-FACING FEATURES (saved jobs, saved searches, notifications)
-- =============================================================================

CREATE TABLE public.saved_jobs (
    id              serial4 NOT NULL,
    user_id         int4 NOT NULL,
    job_listing_id  int4 NOT NULL,
    is_hidden       bool DEFAULT false NOT NULL,  -- user can hide a job from results
    created_at      timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT saved_jobs_pkey PRIMARY KEY (id),
    CONSTRAINT unique_saved_job UNIQUE (user_id, job_listing_id),
    CONSTRAINT saved_jobs_user_fkey FOREIGN KEY (user_id)
        REFERENCES public.users(id) ON DELETE CASCADE,
    CONSTRAINT saved_jobs_job_fkey FOREIGN KEY (job_listing_id)
        REFERENCES public.joblistings(id) ON DELETE CASCADE
);
CREATE INDEX idx_saved_jobs_user ON public.saved_jobs USING btree (user_id);
CREATE INDEX idx_saved_jobs_job  ON public.saved_jobs USING btree (job_listing_id);

-- Stores serialized search filter state as JSONB.
-- Flexible enough to handle any combination of filters without schema changes.
CREATE TABLE public.saved_searches (
    id              serial4 NOT NULL,
    user_id         int4 NOT NULL,
    name            varchar(255) NOT NULL,
    filters         jsonb NOT NULL,         -- e.g. {"city_id":1,"function_id":3,"remote":true}
    notify_on_match bool DEFAULT false NOT NULL,
    last_run_at     timestamp NULL,
    created_at      timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at      timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT saved_searches_pkey PRIMARY KEY (id),
    CONSTRAINT saved_searches_user_fkey FOREIGN KEY (user_id)
        REFERENCES public.users(id) ON DELETE CASCADE
);
CREATE INDEX idx_saved_searches_user   ON public.saved_searches USING btree (user_id);
CREATE INDEX idx_saved_searches_notify ON public.saved_searches USING btree (notify_on_match)
    WHERE notify_on_match = true;

CREATE TRIGGER update_saved_searches_updated_at
    BEFORE UPDATE ON public.saved_searches
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TABLE public.notifications (
    id          serial4 NOT NULL,
    user_id     int4 NOT NULL,
    type        varchar(50) NOT NULL,   -- 'new_job_match','application_update','invite', etc.
    title       varchar(255) NOT NULL,
    body        text NULL,
    is_read     bool DEFAULT false NOT NULL,
    related_job_id  int4 NULL,
    related_company_id int4 NULL,
    related_application_id int4 NULL,
    created_at  timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT notifications_pkey PRIMARY KEY (id),
    CONSTRAINT notifications_user_fkey FOREIGN KEY (user_id)
        REFERENCES public.users(id) ON DELETE CASCADE,
    CONSTRAINT notifications_job_fkey FOREIGN KEY (related_job_id)
        REFERENCES public.joblistings(id) ON DELETE SET NULL,
    CONSTRAINT notifications_company_fkey FOREIGN KEY (related_company_id)
        REFERENCES public.company(id) ON DELETE SET NULL,
    CONSTRAINT notifications_application_fkey FOREIGN KEY (related_application_id)
        REFERENCES public.applications(id) ON DELETE SET NULL
);
CREATE INDEX idx_notifications_user    ON public.notifications USING btree (user_id);
CREATE INDEX idx_notifications_unread  ON public.notifications USING btree (user_id, is_read)
    WHERE is_read = false;
CREATE INDEX idx_notifications_type    ON public.notifications USING btree (type);


-- =============================================================================
-- SCRAPING LOG
-- =============================================================================

CREATE TABLE public.scrapinglog (
    id              serial4 NOT NULL,
    job_board       varchar(100) NOT NULL,
    company_id      int4 NULL,
    jobs_found      int4 DEFAULT 0 NOT NULL,
    jobs_added      int4 DEFAULT 0 NOT NULL,
    jobs_updated    int4 DEFAULT 0 NOT NULL,
    jobs_skipped    int4 DEFAULT 0 NOT NULL,
    errors          text NULL,
    status          varchar(20) DEFAULT 'running' NOT NULL,
    started_at      timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    completed_at    timestamp NULL,
    duration_seconds int4 NULL,
    scraper_version varchar(50) NULL,
    ai_model_used   varchar(50) NULL,
    notes           text NULL,
    CONSTRAINT scrapinglog_pkey PRIMARY KEY (id),
    CONSTRAINT scrapinglog_status_check CHECK (
        status IN ('running','completed','failed','cancelled')
    ),
    CONSTRAINT valid_scraping_counts CHECK (
        jobs_found >= 0 AND jobs_added >= 0 AND jobs_updated >= 0 AND jobs_skipped >= 0
    ),
    CONSTRAINT logical_scraping_counts CHECK (
        (jobs_added + jobs_updated + jobs_skipped) <= jobs_found
    ),
    CONSTRAINT valid_scraping_timing CHECK (
        (completed_at IS NULL) OR (completed_at >= started_at)
    ),
    CONSTRAINT scrapinglog_company_id_fkey FOREIGN KEY (company_id)
        REFERENCES public.company(id) ON DELETE SET NULL
);
CREATE INDEX idx_scrapinglog_company   ON public.scrapinglog USING btree (company_id);
CREATE INDEX idx_scrapinglog_job_board ON public.scrapinglog USING btree (job_board);
CREATE INDEX idx_scrapinglog_started   ON public.scrapinglog USING btree (started_at DESC);
CREATE INDEX idx_scrapinglog_status    ON public.scrapinglog USING btree (status);

CREATE TRIGGER update_scrapinglog_duration
    BEFORE UPDATE ON public.scrapinglog
    FOR EACH ROW EXECUTE FUNCTION update_scraping_duration();


-- =============================================================================
-- SITE FEATURES (company site feature flags — unchanged from original)
-- =============================================================================

CREATE TABLE public.sitefeatures (
    id          serial4 NOT NULL,
    site_id     int4 NOT NULL,
    feature_id  int4 NOT NULL,
    created_at  timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT sitefeatures_pkey PRIMARY KEY (id),
    CONSTRAINT unique_site_feature UNIQUE (site_id, feature_id),
    CONSTRAINT sitefeatures_featureid_fkey FOREIGN KEY (feature_id)
        REFERENCES public.features(id) ON DELETE CASCADE,
    CONSTRAINT sitefeatures_siteid_fkey FOREIGN KEY (site_id)
        REFERENCES public.companysite(id) ON DELETE CASCADE
);


-- =============================================================================
-- VIEWS
-- =============================================================================

CREATE OR REPLACE VIEW public.companyprofile AS
SELECT
    c.id,
    c.common_name AS company_name,
    c.legal_name,
    c.website,
    c.jobboard,
    ct.name AS company_type,
    c.date_founded AS founded_date,
    pi.name AS primary_industry,
    pf.name AS primary_function,
    hq_city.city_name AS hq_city,
    hq_state.name AS hq_state,
    hq_country.name AS hq_country,
    c.is_scraped,
    (
        SELECT count(*) FROM joblistings
        WHERE joblistings.company_id = c.id
          AND joblistings.job_status_id = 1
          AND joblistings.approved = true
    ) AS active_jobs,
    (
        SELECT count(*) FROM joblistings
        WHERE joblistings.company_id = c.id
    ) AS total_jobs,
    c.created_at,
    c.updated_at
FROM company c
LEFT JOIN company_type ct          ON c.company_type = ct.id
LEFT JOIN companyindustries ci     ON c.id = ci.company_id
LEFT JOIN industries pi            ON ci.industry_id = pi.id
LEFT JOIN companyfunctions cf      ON c.id = cf.company_id
LEFT JOIN functions pf             ON cf.function_id = pf.id
LEFT JOIN companysite hq           ON c.id = hq.company_id AND hq.is_headquarters = true
LEFT JOIN cities hq_city           ON hq.city_id = hq_city.id
LEFT JOIN state hq_state           ON hq.state_id = hq_state.id
LEFT JOIN country hq_country       ON hq.country_id = hq_country.id
WHERE c.approved = true;


-- Pending approval queues — useful for moderator dashboard
CREATE OR REPLACE VIEW public.pending_companies AS
SELECT c.id, c.common_name, c.legal_name, c.website, c.created_at,
       u.email AS submitted_by_email, u.full_name AS submitted_by_name
FROM company c
LEFT JOIN user_company_roles ucr ON ucr.company_id = c.id AND ucr.role = 'company_admin'
LEFT JOIN users u ON ucr.user_id = u.id
WHERE c.approved = false
ORDER BY c.created_at ASC;

CREATE OR REPLACE VIEW public.pending_joblistings AS
SELECT jl.id, jl.job_title, jl.created_at,
       c.common_name AS company_name,
       u.email AS posted_by_email,
       jl.application_method
FROM joblistings jl
JOIN company c ON jl.company_id = c.id
LEFT JOIN users u ON jl.posted_by = u.id
WHERE jl.approved = false
ORDER BY jl.created_at ASC;

CREATE OR REPLACE VIEW public.pending_user_company_roles AS
SELECT ucr.id, ucr.role, ucr.created_at,
       u.email AS user_email, u.full_name AS user_name,
       c.common_name AS company_name
FROM user_company_roles ucr
JOIN users u   ON ucr.user_id = u.id
JOIN company c ON ucr.company_id = c.id
WHERE ucr.approved = false
ORDER BY ucr.created_at ASC;


-- =============================================================================
-- USER PROFILE — SKILLS & CERTIFICATIONS
-- Users can build a profile with claimed skills and certifications.
-- Skills reference the same taxonomy used by job listings, enabling matching.
-- Certifications reference the same table used by job listing requirements.
-- =============================================================================

-- Skills a user claims to have.
-- Proficiency and years self-reported; no verification at this stage.
CREATE TABLE public.user_skills (
    id                serial4 NOT NULL,
    user_id           int4 NOT NULL,
    skill_id          int4 NOT NULL,
    proficiency_level varchar(20) DEFAULT 'unknown' NULL,
    years_experience  int4 NULL,
    is_featured       bool DEFAULT false NOT NULL,  -- user can highlight top skills
    created_at        timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at        timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT user_skills_pkey PRIMARY KEY (id),
    CONSTRAINT unique_user_skill UNIQUE (user_id, skill_id),
    CONSTRAINT valid_user_proficiency CHECK (
        proficiency_level IN ('beginner','intermediate','advanced','expert','unknown')
    ),
    CONSTRAINT valid_user_years CHECK (
        (years_experience IS NULL) OR (years_experience >= 0)
    ),
    CONSTRAINT user_skills_user_fkey FOREIGN KEY (user_id)
        REFERENCES public.users(id) ON DELETE CASCADE,
    CONSTRAINT user_skills_skill_fkey FOREIGN KEY (skill_id)
        REFERENCES public.skills(id) ON DELETE CASCADE
);
CREATE INDEX idx_user_skills_user  ON public.user_skills USING btree (user_id);
CREATE INDEX idx_user_skills_skill ON public.user_skills USING btree (skill_id);

CREATE TRIGGER update_user_skills_updated_at
    BEFORE UPDATE ON public.user_skills
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();


-- Certifications a user holds.
-- verified=false means self-reported; admin or future integration can flip it.
CREATE TABLE public.user_certifications (
    id               serial4 NOT NULL,
    user_id          int4 NOT NULL,
    certification_id int4 NOT NULL,
    obtained_date    date NULL,
    expiry_date      date NULL,
    credential_id    varchar(255) NULL,   -- cert number/ID as issued
    credential_url   varchar(500) NULL,   -- link to verifiable credential
    verified         bool DEFAULT false NOT NULL,
    verified_by      int4 NULL,           -- user_id of admin who verified
    verified_at      timestamp NULL,
    created_at       timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at       timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT user_certifications_pkey PRIMARY KEY (id),
    CONSTRAINT unique_user_certification UNIQUE (user_id, certification_id),
    CONSTRAINT valid_cert_dates CHECK (
        (expiry_date IS NULL) OR (obtained_date IS NULL) OR (expiry_date > obtained_date)
    ),
    CONSTRAINT valid_credential_url CHECK (
        (credential_url IS NULL) OR ((credential_url)::text ~* '^https?://')
    ),
    CONSTRAINT user_certifications_user_fkey FOREIGN KEY (user_id)
        REFERENCES public.users(id) ON DELETE CASCADE,
    CONSTRAINT user_certifications_cert_fkey FOREIGN KEY (certification_id)
        REFERENCES public.certifications(id) ON DELETE CASCADE,
    CONSTRAINT user_certifications_verified_by_fkey FOREIGN KEY (verified_by)
        REFERENCES public.users(id) ON DELETE SET NULL
);
CREATE INDEX idx_user_certifications_user ON public.user_certifications USING btree (user_id);
CREATE INDEX idx_user_certifications_cert ON public.user_certifications USING btree (certification_id);
CREATE INDEX idx_user_certifications_verified ON public.user_certifications USING btree (verified);

CREATE TRIGGER update_user_certifications_updated_at
    BEFORE UPDATE ON public.user_certifications
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();


-- Certifications mentioned in a job listing (required or preferred).
-- Parallel to joblistingskills.
CREATE TABLE public.joblistingcertifications (
    id                  serial4 NOT NULL,
    job_listing_id      int4 NOT NULL,
    certification_id    int4 NOT NULL,
    is_required         bool DEFAULT false NOT NULL,
    is_preferred        bool DEFAULT false NOT NULL,
    extraction_method   varchar(20) DEFAULT 'manual' NULL,
    confidence_score    numeric(3,2) NULL,
    created_at          timestamp DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT joblistingcertifications_pkey PRIMARY KEY (id),
    CONSTRAINT unique_job_certification UNIQUE (job_listing_id, certification_id),
    CONSTRAINT valid_cert_extraction_method CHECK (
        extraction_method IN ('manual','ai','hybrid')
    ),
    CONSTRAINT valid_cert_confidence CHECK (
        (confidence_score IS NULL) OR
        (confidence_score >= 0 AND confidence_score <= 1)
    ),
    CONSTRAINT jlc_job_fkey FOREIGN KEY (job_listing_id)
        REFERENCES public.joblistings(id) ON DELETE CASCADE,
    CONSTRAINT jlc_cert_fkey FOREIGN KEY (certification_id)
        REFERENCES public.certifications(id) ON DELETE CASCADE
);
CREATE INDEX idx_jlc_job  ON public.joblistingcertifications USING btree (job_listing_id);
CREATE INDEX idx_jlc_cert ON public.joblistingcertifications USING btree (certification_id);


-- =============================================================================
-- VIEW: skill demand — how often each skill appears in active listings
-- Useful for "most in-demand skills in Tulsa right now" community feature
-- =============================================================================

CREATE OR REPLACE VIEW public.skill_demand AS
SELECT
    s.id AS skill_id,
    s.name AS skill_name,
    sc.name AS category,
    count(jls.job_listing_id) AS listing_count,
    count(jls.job_listing_id) FILTER (WHERE jls.required_skill = true) AS required_count,
    count(jls.job_listing_id) FILTER (WHERE jls.preferred_skill = true) AS preferred_count
FROM skills s
LEFT JOIN skill_categories sc ON s.skill_category_id = sc.id
LEFT JOIN joblistingskills jls ON jls.skill_id = s.id
LEFT JOIN joblistings jl ON jls.job_listing_id = jl.id
    AND jl.approved = true
    AND jl.job_status_id = 1
GROUP BY s.id, s.name, sc.name
ORDER BY listing_count DESC;
