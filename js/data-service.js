/**
 * DataService
 * Handles data fetching from either Local storage or GitHub API
 */
class DataService {
    constructor() {
        this.config = {
            sourceType: 'local', // 'local' | 'remote'
            githubToken: '',
            owner: '',
            repo: '',
            branch: 'main',
            path: 'data' // Path to data folder in repo
        };

        this.loadConfig();
    }

    // ===========================================
    // CONFIGURATION
    // ===========================================
    loadConfig() {
        const stored = localStorage.getItem('portfolio_config');
        if (stored) {
            try {
                const parsed = JSON.parse(stored);
                this.config = { ...this.config, ...parsed };
                // Ensure defaults
                if (!this.config.branch) this.config.branch = 'main';
                if (!this.config.path) this.config.path = 'data';
            } catch (e) {
                console.error('Failed to parse config', e);
            }
        }
    }

    saveConfig(newConfig) {
        this.config = { ...this.config, ...newConfig };
        localStorage.setItem('portfolio_config', JSON.stringify(this.config));
    }

    resetConfig() {
        this.config = {
            sourceType: 'local',
            githubToken: '',
            owner: '',
            repo: '',
            branch: 'main',
            path: 'data'
        };
        localStorage.removeItem('portfolio_config');
    }

    isRemote() {
        return this.config.sourceType === 'remote';
    }

    isConfigured() {
        if (!this.isRemote()) return true;
        return this.config.githubToken && this.config.owner && this.config.repo;
    }

    // ===========================================
    // CORE FETCHING
    // ===========================================

    // Core internal fetcher that handles the logic
    async _fetch(filename) {
        if (this.isRemote()) {
            return await this._fetchRemote(filename);
        } else {
            return await this._fetchLocal(filename);
        }
    }

    async _fetchLocal(filename) {
        // Cache busting for local files
        const url = `./data/${filename}?t=${Date.now()}`;
        try {
            const response = await fetch(url);
            if (!response.ok) return null;
            return await response.json();
        } catch (e) {
            console.warn(`Local fetch failed for ${filename}`, e);
            return null;
        }
    }

    async _fetchRemote(filename) {
        if (!this.isConfigured()) return null;

        // Construct GitHub API URL
        // GET /repos/{owner}/{repo}/contents/{path}
        const filePath = this.config.path ? `${this.config.path}/${filename}` : filename;
        const apiUrl = `https://api.github.com/repos/${this.config.owner}/${this.config.repo}/contents/${filePath}?ref=${this.config.branch}&t=${Date.now()}`;

        try {
            const response = await fetch(apiUrl, {
                headers: {
                    'Authorization': `token ${this.config.githubToken}`,
                    'Accept': 'application/vnd.github.v3+json'
                }
            });

            if (response.status === 404) return null;
            if (response.status === 401 || response.status === 403) {
                throw new Error(`Auth Error: ${response.status} ${response.statusText}`);
            }

            if (!response.ok) {
                throw new Error(`GitHub API Error: ${response.status}`);
            }

            const data = await response.json();

            // GitHub Content API returns base64 encoded content
            if (data.encoding === 'base64' && data.content) {
                // Decode base64 (handling UTF-8 correctly)
                const jsonString = new TextDecoder().decode(
                    Uint8Array.from(atob(data.content.replace(/\n/g, '')), c => c.charCodeAt(0))
                );
                return JSON.parse(jsonString);
            }

            throw new Error('Unexpected content format from GitHub');

        } catch (e) {
            console.error(`Remote fetch failed for ${filename}`, e);
            throw e; // Propagate auth errors
        }
    }

    // ===========================================
    // PUBLIC API
    // ===========================================

    async fetchPortfolioData(monthId) {
        return await this._fetch(`${monthId}.json`);
    }

    async fetchCategories() {
        return await this._fetch('categories.json');
    }

    async fetchTransfersData(filename) {
        const data = await this._fetch(filename);
        if (!data) return null;

        if (data.transfers) {
            return data;
        }
        return { meta: {}, transfers: data };
    }

    async checkFileExists(filename) {
        if (this.isRemote()) {
            // For remote, we can just try to fetch metadata or check the file list
            // Optimization: If we already listed files, we could check that list.
            // But for simplicity, we'll do a lightweight fetch if possible, or just HEAD.
            // GitHub API doesn't support HEAD on contents straightforwardly without cost.
            // Best is to use the file listing from listAvailableMonths if possible, 
            // but for specific file checks, we might need a direct call.
            try {
                const data = await this._fetchRemote(filename);
                return !!data;
            } catch (e) {
                return false;
            }
        } else {
            // Local HEAD check
            try {
                const response = await fetch(`./data/${filename}`, { method: 'HEAD' });
                return response.ok;
            } catch (e) {
                return false;
            }
        }
    }

    // LIST AVAILABLE MONTHS
    // Returns array of { id: '2024-01', label: 'Month Year' }
    async getAvailableMonths(candidateGenerator) {
        if (this.isRemote() && this.isConfigured()) {
            return await this._getAvailableMonthsRemote();
        } else {
            return await this._getAvailableMonthsLocal(candidateGenerator);
        }
    }

    async _getAvailableMonthsLocal(candidateGenerator) {
        const candidates = candidateGenerator();
        // Check all local files
        const checks = candidates.map(async (month) => {
            const exists = await this.checkFileExists(`${month.id}.json`);
            return exists ? month : null;
        });
        const results = await Promise.all(checks);
        return results.filter(m => m !== null);
    }

    async _getAvailableMonthsRemote() {
        try {
            // List contents of the data directory
            const dirPath = this.config.path || '';
            const apiUrl = `https://api.github.com/repos/${this.config.owner}/${this.config.repo}/contents/${dirPath}?ref=${this.config.branch}`;

            const response = await fetch(apiUrl, {
                headers: {
                    'Authorization': `token ${this.config.githubToken}`,
                    'Accept': 'application/vnd.github.v3+json'
                }
            });

            if (!response.ok) throw new Error('Failed to list directory');

            const files = await response.json();
            if (!Array.isArray(files)) return [];

            // Filter for YYYY-MM.json files
            const monthRegex = /^(\d{4})-(\d{2})\.json$/;

            const available = [];
            files.forEach(file => {
                const match = file.name.match(monthRegex);
                if (match) {
                    const id = file.name.replace('.json', '');
                    const year = parseInt(match[1]);
                    const monthIndex = parseInt(match[2]) - 1; // 0-based

                    // Utilize the global MONTH_NAMES if available, or simplified fallback
                    const monthName = (typeof MONTH_NAMES !== 'undefined')
                        ? MONTH_NAMES[monthIndex]
                        : new Date(year, monthIndex).toLocaleString('ru-RU', { month: 'long' });

                    // Capitalize first letter
                    const label = `${monthName.charAt(0).toUpperCase() + monthName.slice(1)} ${year}`;

                    available.push({ id, label });
                }
            });

            // Sort by ID
            return available.sort((a, b) => a.id.localeCompare(b.id));

        } catch (e) {
            console.error('Remote listing failed', e);
            throw e;
        }
    }

    async testConnection() {
        if (!this.isConfigured()) {
            return { success: false, message: 'Configuration incomplete' };
        }

        try {
            // Try to fetch the user profile to verify token
            const userRes = await fetch('https://api.github.com/user', {
                headers: { 'Authorization': `token ${this.config.githubToken}` }
            });

            if (!userRes.ok) {
                if (userRes.status === 401) return { success: false, message: 'Invalid Token' };
                return { success: false, message: `GitHub User API: ${userRes.status}` };
            }

            // Try to fetch the repo (confirms access rights)
            const repoRes = await fetch(`https://api.github.com/repos/${this.config.owner}/${this.config.repo}`, {
                headers: { 'Authorization': `token ${this.config.githubToken}` }
            });

            if (!repoRes.ok) {
                if (repoRes.status === 404) return { success: false, message: 'Repository not found or no access' };
                return { success: false, message: `Repo API: ${repoRes.status}` };
            }

            return { success: true, message: 'Connected successfully' };

        } catch (e) {
            return { success: false, message: e.message };
        }
    }
}

// Export singleton
const dataService = new DataService();
