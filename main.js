'use strict';
const utils = require('@iobroker/adapter-core');
const {Controller} = require('./lib/controller');

class AirPlayMM extends utils.Adapter {
    constructor(options) {
        super({...options, name: 'airplay-mm'});
        this.controller = new Controller(this, utils);
        this.on('ready', () => this.controller.start().catch(error => this.controller.failure(error)));
        this.on('stateChange', (id, state) => this.controller.stateChange(id, state));
        this.on('message', message => this.controller.message(message));
        this.on('unload', callback => this.controller.stop().catch(error => this.log.warn(String(error.message || error))).finally(callback));
    }
}
if (require.main === module) new AirPlayMM();
else module.exports = options => new AirPlayMM(options);
